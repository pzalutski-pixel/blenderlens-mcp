#!/usr/bin/env node
// Launcher for the BlenderLens MCP server. The package carries the server's Python source, which needs
// nothing beyond Python's standard library. This file finds Python 3.10 or later on the machine and runs the
// server with stdio inherited, so the MCP client talks to the server directly.
//
// stdout carries the MCP protocol, so this file never writes to it. Every message of its own goes to
// stderr.
"use strict";

const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

// The layout that `.github/scripts/release_kit.py npm-bundle` builds in the workflows:
//   <package>/server/blenderlens_mcp/{__main__,cli,server,session,tools,...}.py and addon/
// PYTHONPATH must name <package>/server, the parent of the package folder, or the server cannot import its
// own package. CI's package job and the release workflow install the built tarball and run it, which is what
// shows a wrong path.
const SERVER_DIR = path.join(__dirname, "..", "server");
const PACKAGE_DIR = path.join(SERVER_DIR, "blenderlens_mcp");
const ENTRY_POINT = path.join(PACKAGE_DIR, "__main__.py");
const ISSUES = "https://github.com/pzalutski-pixel/blenderlens-mcp/issues";

// The signals a parent process is expected to pass on to its child. SIGHUP matters when a client closes
// the terminal that started it: without forwarding, the server and the background Blender it owns would
// outlive the terminal.
const FORWARDED_SIGNALS = ["SIGINT", "SIGTERM", "SIGHUP"];

// Each candidate runs this one-line program, which prints its version. Only running code shows that an
// interpreter works: on Windows, `python3` and `python` are often the Microsoft Store's placeholders, which
// exit without running anything. The py launcher is how a python.org install is found there.
const VERSION_PROBE = "import sys; print('%d.%d' % sys.version_info[:2])";

// The file extensions that can be spawned without a shell on Windows. A .cmd or .bat file needs cmd.exe,
// which searches the current working directory again and parses the arguments again, and libuv does not run such files
// for a bare program name either.
const WINDOWS_RUNNABLE = new Set([".com", ".exe"]);
const WINDOWS_DEFAULT_PATHEXT = ".COM;.EXE";

/** Return the interpreters to try, in order, each as [command, ...arguments before the script]. */
function candidates(platform = process.platform) {
  const list = [["python3"], ["python"]];
  if (platform === "win32") list.push(["py", "-3"]);
  return list;
}

/** Return an environment variable by name. On Windows the name's case does not matter, and `Path` is common. */
function envValue(env, name, platform) {
  if (platform !== "win32") return env[name];
  const key = Object.keys(env).find((k) => k.toUpperCase() === name);
  return key === undefined ? undefined : env[key];
}

/** Return whether `file` can be run: a file, or on Windows an app execution alias, but never a folder. */
function isRunnable(file, platform) {
  let entry;
  try {
    entry = fs.lstatSync(file);
  } catch {
    return false;
  }
  if (entry.isDirectory()) return false;
  try {
    if (!fs.statSync(file).isFile()) return false;
  } catch {
    // The Microsoft Store's python.exe is an app execution alias: a reparse point that stat cannot follow
    // (it fails with EACCES, and existsSync returns false), yet Windows runs it, and for some users it is
    // their only Python. The version probe tells a working one from the placeholder that only opens the Store.
    if (!(platform === "win32" && entry.isSymbolicLink())) return false;
  }
  if (platform === "win32") return true;
  try {
    fs.accessSync(file, fs.constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

/**
 * Return the absolute path that `name` runs as, found by searching PATH here, or null when it is not on PATH.
 *
 * The result is never a bare name, because spawning a bare name hands the search to libuv, which on Windows
 * looks in the current working directory before PATH: a python.exe in the project the MCP client was started in would
 * then run in place of Python. On POSIX, execvp reads an empty PATH entry as the current working directory. So empty,
 * "." and relative entries, which all name the current working directory or a folder under it, are skipped.
 */
function resolveCommand(name, env = process.env, platform = process.platform) {
  const paths = platform === "win32" ? path.win32 : path.posix;
  const separator = platform === "win32" ? ";" : ":";
  let extensions = [""];
  if (platform === "win32") {
    const listed = String(envValue(env, "PATHEXT", platform) || WINDOWS_DEFAULT_PATHEXT).split(";");
    extensions = listed.map((ext) => ext.trim().toLowerCase()).filter((ext) => WINDOWS_RUNNABLE.has(ext));
  }
  for (const raw of String(envValue(env, "PATH", platform) || "").split(separator)) {
    // Windows allows a PATH entry in quotes, and the quotes are not part of the folder's name.
    const folder = platform === "win32" ? raw.trim().replace(/^"(.*)"$/, "$1") : raw;
    if (!folder || folder === "." || !paths.isAbsolute(folder)) continue;
    // isAbsolute accepts "\folder", but that is rooted on whichever drive is current, so it is not a fixed place.
    if (platform === "win32" && !/^([A-Za-z]:[\\/]|[\\/]{2})/.test(folder)) continue;
    for (const ext of extensions) {
      const file = paths.join(folder, name + ext);
      if (isRunnable(file, platform)) return file;
    }
  }
  return null;
}

/**
 * Find the first candidate that runs and reports Python 3.10 or later.
 *
 * Return { found, tooOld }. `found` is { command, prefix }, with `command` an absolute path, or null when no
 * candidate qualifies. `tooOld` describes the interpreters that ran but were older, for the error message.
 */
function findPython(platform = process.platform, run = spawnSync, env = process.env) {
  const tooOld = [];
  for (const [name, ...prefix] of candidates(platform)) {
    const command = resolveCommand(name, env, platform);
    if (!command) continue;
    const probe = run(command, [...prefix, "-c", VERSION_PROBE], {
      encoding: "utf-8",
      stdio: ["ignore", "pipe", "ignore"],
      timeout: 15000,
      windowsHide: true,
    });
    if (probe.error || probe.status !== 0) continue;
    const match = /^(\d+)\.(\d+)$/.exec(String(probe.stdout || "").trim());
    if (!match) continue;
    const major = Number(match[1]);
    const minor = Number(match[2]);
    if (major === 3 && minor >= 10) return { found: { command, prefix }, tooOld };
    tooOld.push(`${[command, ...prefix].join(" ")} is Python ${major}.${minor}`);
  }
  return { found: null, tooOld };
}

/** Return the exit status to pass on: the child's exit code, or 128 plus the number of the signal that ended it. */
function exitCodeFor(code, signal) {
  if (typeof code === "number") return code;
  if (!signal) return 0;
  return 128 + (os.constants.signals[signal] ?? 1);
}

/**
 * Pass SIGINT, SIGTERM and SIGHUP on to the child, and stop listening once the child is gone, so the
 * launcher never keeps itself alive. The server decides what a signal means, and the launcher only makes
 * sure it is not swallowed. Return a function that stops the forwarding.
 */
function forwardSignals(child, proc = process, signals = FORWARDED_SIGNALS) {
  const handlers = signals.map((signal) => {
    const handler = () => {
      try {
        child.kill(signal);
      } catch {
        // The child is already gone, and its 'exit' event decides the exit status.
      }
    };
    proc.on(signal, handler);
    return { signal, handler };
  });
  const stop = () => {
    for (const { signal, handler } of handlers) proc.removeListener(signal, handler);
  };
  child.once("exit", stop);
  return stop;
}

/** Write `message` to stderr and exit with status 1. */
function fail(message) {
  process.stderr.write(message);
  process.exit(1);
}

/**
 * Check that the bundled server is in place, find Python, and run the server with this process's arguments
 * and stdio, exiting with the server's own exit status.
 */
function main() {
  if (!fs.existsSync(ENTRY_POINT)) {
    fail(
      "Error: the blenderlens-mcp server files are missing or misplaced.\n" +
        `Expected the entry point at: ${ENTRY_POINT}\n` +
        "The package install is broken. Reinstall it, and if the problem persists, report it at\n" +
        `${ISSUES}\n`
    );
  }

  const { found, tooOld } = findPython();
  if (!found) {
    fail(
      "Error: BlenderLens needs Python 3.10 or later, and none was found.\n" +
        (tooOld.length ? `Found: ${tooOld.join("; ")}.\n` : "") +
        `Tried: ${candidates().map((c) => c.join(" ")).join(", ")}.\n` +
        "Install Python from https://www.python.org/downloads/ and make sure it is on PATH.\n"
    );
  }

  // Run the script by its path rather than with `-m`: Python then puts the script's own folder, which holds
  // only BlenderLens's modules, first on sys.path. With -m it would put the client's current working directory there,
  // where a project's own json.py or socket.py would shadow the standard library.
  const child = spawn(found.command, [...found.prefix, ENTRY_POINT, ...process.argv.slice(2)], {
    stdio: "inherit",
    env: { ...process.env, PYTHONPATH: SERVER_DIR },
    windowsHide: true,
  });
  forwardSignals(child);
  child.on("exit", (code, signal) => process.exit(exitCodeFor(code, signal)));
  child.on("error", (err) => fail(`Error: failed to start ${found.command}: ${err.message}\n`));
}

if (require.main === module) main();

module.exports = {
  candidates,
  resolveCommand,
  findPython,
  exitCodeFor,
  forwardSignals,
  SERVER_DIR,
  ENTRY_POINT,
};
