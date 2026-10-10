# 0032 — Preview options: sheets, context, styles, markers, cutaways, a fixed camera
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision

`render_preview` takes six parameters beyond framing and rendering, listed here from the most often needed. All
six fit within the size cap of the tool list (decision 0020).

- **`views`: a contact sheet in one PNG.** The caller names 1 to 8 views. The add-on renders them one after
  another, each at `resolution`, and lays them out left to right and top to bottom in the order given: in one
  row for up to three views, and otherwise as near a square as holds them (four make 2 x 2, five or six make
  3 x 2). The result adds `views`, each with its angles, its camera and its `cell` (`[x, y, width, height]` in
  the sheet), and `resolution` becomes the sheet's. `view`, `azimuth` and `elevation` describe a single view, so
  they are refused beside `views`. The views are not labelled in the image: the cells follow the order asked
  for, and the result says where each one is.
  - **numpy, in this one function.** Each view is written to a temporary PNG and read back with
    `Image.pixels.foreach_get` into a numpy array. The add-on places the arrays and writes the sheet itself as
    an 8-bit PNG (zlib, colour type 2, or 6 with `transparent`). An 8-bit PNG's pixels come back as its bytes
    divided by 255, so the sheet holds exactly the bytes each view rendered. numpy ships inside Blender, and
    nothing else in the add-on uses it.
  - **Bounds.** Views count toward the cost bound: views x width x height x samples must stay within 1.1e9 for
    cycles and eevee (the bound of decision 0013, taken over all views). The sheet itself must stay within 4096
    pixels on each side, for every engine. Both refusals name their limit.
- **`context`: shown, not framed.** These objects, with their children, stay visible beside the framed ones, so
  they appear in the picture and cast and catch shadows. The framing, the camera and `bounds` come from
  `objects` alone. The clip planes are widened to hold the context objects.
- **`style`** is one of `shaded` (the default); `clay` (Workbench, matcap lighting, one light grey, cavity in both
  world and screen space); `random` (Workbench, a colour per object); or `wireframe` (any engine, with the edges
  drawn over the faces). `clay` and `random` are Workbench looks, so they render with Workbench whatever
  `engine` says, and the `engine` in the result says so.
  - **The wireframe is an overlay.** Workbench ignores an object's display type. So for each framed object the
    add-on makes a new mesh from the object as evaluated, gives it a Wireframe modifier (replace, even thickness)
    and a near-black material (setting its `diffuse_color` too, which Workbench draws), and places it where the
    object is. The wire thickness is 1.5 pixels of the picture, set for each view. In an orthographic view it
    comes from the camera's own `ortho_scale` over the picture's longer side. In a perspective view it comes from
    the width the horizontal field of view spans at the depth of the nearest framed corner, over the picture's
    width. The objects themselves are not touched, and the copies, their meshes and the material are removed
    afterwards.
- **`markers`: where objects land.** For each object named, the result gives its origin projected through the
  preview's camera (`bpy_extras.object_utils.world_to_camera_view`), as pixels from the PNG's top left, with
  `in_frame`. On a sheet it gives one marker per view, offset into that view's cell. Pixels are square for the
  preview (`pixel_aspect` is set to 1 and put back afterwards), so the numbers are the PNG's.
- **`hide_from_camera`: a cutaway that keeps its shadows.** Each object named has `visible_camera` turned off for
  the render: the camera does not see it, but it still casts shadows and shows in reflections (in cycles and
  eevee). Workbench ignores ray visibility, and draws shadows only when its own shadow option is on (it is off
  by default), so there the object is simply hidden.
- **`ortho_scale`: a fixed camera.** It is the width an orthographic camera sees, in metres (0.001 to 1e6). It
  implies `projection: orthographic` and is refused with `perspective`. With it, a metre covers the same number
  of pixels in every preview, whatever is framed.
- **Orthographic framing fits what the camera sees.** Without `ortho_scale`, the add-on projects the corners of
  the framed objects' evaluated bounds onto the camera's own right and up axes, centres the camera on that
  rectangle, and sets its scale to fit it with `margin`.
- **Perspective framing fits both fields of view.** The camera stands as far back as the most demanding corner
  needs. A corner `z` toward the camera and `x` across fits at z + |x| margin / tan of half the horizontal field
  of view, and likewise for the vertical one (with `sensor_fit` AUTO the sensor spans the picture's longer
  side). Four passes then move the camera sideways by how far the corners' projections lean, since near parts
  look larger, and the distance is fitted again. Each view, and each sheet cell, has its own distance. The wire
  overlay's width is taken at the nearest corner's depth, where edges look largest.
- **Everything is put back.** Every setting goes through the preview's restore list. The temporary camera,
  lights, world, wire copies and their material are removed. The temporary view PNGs and their folder are
  deleted. A "Render Result" image that the render made is removed, unless one was there before.

## Why

The six serve what a review of a model in pictures commonly needs. A sheet of several views shows a model from
every side at a glance. Context objects, such as a floor or a neighbouring part, show its scale and its fit.
Matcap clay and wire overlays show the shape apart from its materials. Markers say where each part lands in the
picture, to label it or to check its place. A cutaway shows what a shell hides. A fixed orthographic camera
matches the camera of a game that uses one, so the preview shows the model at the size a player sees it. One
sheet instead of four PNGs is one file for the agent to open, and one image in its context instead of four.

- **A sheet's cost** is the sum of its views' costs, which is why the bound of decision 0013 is taken over all
  the views of a sheet.
- **Sphere framing wastes an orthographic picture.** A pole 10 m long seen end on is a 0.2 m square, but the
  sphere around it is 10 m across, so framed by that sphere it is a pixel wide. Projected onto the camera's axes,
  it fills the picture. The test counts pixels: more than 60 % covered, where sphere framing covers under 1 %.
- **Sphere framing also wastes a wide perspective picture.** Fitting a sphere around the framed objects to the
  picture's narrow side, a row of objects 2.2 m wide in 768 x 320 cells puts the camera 8.3 m back and fills a
  third of the cell. Fitted to both fields of view, the camera stands at 3.3 m and the row spans most of the
  cell's width. The test renders a long box in a wide picture and a column in a tall one, alone and as sheets,
  and measures the span of the drawn pixels: over 70 % of the limiting side, where sphere framing reaches 37.5 %.
- **Wire thickness per view.** Taking the thickness from the framed sphere in every view would draw the end of a
  10 m pole, which an orthographic view frames tightly, under wires 0.17 m wide, which darken a third of the
  picture. Drawn 1.5 pixels wide, the rim of the pole's end darkens about 5 % of it. The tests compare a
  wireframe render with a shaded one of the same end-on pole in both projections.
- **A bound on each side of the sheet.** A bound on the sheet's pixels alone would let eight views of 4096 x 16
  make a sheet 12,288 pixels wide. The tests refuse a sheet of 8192 x 16 and one of 32 x 8192 before rendering.
- **The Render Result image.** Every render leaves a "Render Result" image in the file, and the snapshot that
  decision 0010 compares would count it as a datablock the preview added.
- **Square pixels.** `world_to_camera_view` reads the scene's pixel aspect, and a file with another aspect would
  stretch the preview and put markers off their objects.
- **Workbench reads `Material.diffuse_color`**, so the wire material sets it as well as its Principled BSDF's
  base colour.

Tests: `tests/test_preview_views.py` (two views make one PNG of twice the width, each cell the same as that view
alone; four make a square sheet, RGBA when transparent; a sheet with every option leaves the scene, its images and
its meshes as they were; no Render Result left; context shows without changing the framing; a marker at the centre
projects to the centre pixel, and on a sheet to each cell's; a fixed `ortho_scale` keeps metres to pixels; an
end-on pole fills an orthographic picture; a long box in a wide picture and a column in a tall one fill the side
that limits them in perspective, alone and in a sheet; a body hidden from the camera still shadows the floor; clay
and random use Workbench and wireframe draws edges, about 1.5 pixels wide in either projection; refusals for mixed
views and the bounds, each side of a sheet among them, before any render).
