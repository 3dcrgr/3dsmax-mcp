# MAXScript: Mesh and Poly Operations Reference

## Conversion & Setup

```maxscript
-- Convert to Editable_Mesh
convertToMesh $obj            -- destroys modifiers, always yields Editable_Mesh
addModifier $obj (meshSelect()); collapseStack $obj  -- alternative
convertTo $obj TriMeshGeometry

-- Convert to Editable_Poly
convertTo $obj Editable_Poly
convertToPoly $obj            -- shorthand

-- Create empty Editable_Poly (no direct constructor)
ep = Editable_Mesh(); convertTo ep Editable_Poly

-- Snapshot (accounts for space warps)
newNode = snapshot $obj
```

## Mesh Creation From Arrays

```maxscript
-- Create mesh from vertex and face arrays
m = mesh vertices:#([0,0,0],[10,0,0],[0,10,0],[10,10,0]) \
    faces:#([1,2,3],[2,4,3]) materialIDs:#(1,2)

-- Build with TriMesh then assign
tri = TriMesh()
setMesh tri numverts:4 numfaces:2
setVert tri 1 [0,0,0]; setVert tri 2 [10,0,0]
setVert tri 3 [0,10,0]; setVert tri 4 [10,10,0]
setFace tri 1 [1,2,3]; setFace tri 2 [2,4,3]
update tri

-- Assign TriMesh to scene node
node = Editable_Mesh()
node.mesh = tri
update node

-- setMesh bulk update (preserves node, resets geometry)
setMesh $obj vertices:#(...) faces:#(...)
```

## CRITICAL: update Must Be Called

```maxscript
-- After ANY mesh modifications, MUST call:
update $obj
-- Failure to call update before viewport redraw = crash risk
-- update args: geometry:true topology:true normals:true (all default true)
```

## Editable_Mesh / TriMesh: Vertex Methods

```maxscript
getNumVerts $obj                      -- vertex count
getVert $obj 1                        -- get position as Point3
setVert $obj 1 [10,5,0]              -- set position
getNormal $obj 1                      -- vertex normal (smoothing-aware)
setNormal $obj 1 [0,0,1]             -- set explicit normal (Max 2015+)
deleteVert $obj 1                     -- delete vertex + connected faces

-- meshop struct (advanced)
meshop.getVert $obj 1 node:$obj      -- position in current coord system
meshop.setVert $obj #{1..10} [0,0,0] -- set multiple (BitArray!)
meshop.moveVert $obj #{1..5} [0,0,10] -- offset vertices
meshop.breakVerts $obj #{1}           -- break shared vertex
meshop.chamferVerts $obj #{13} 3.5
meshop.weldVertsByThreshold $obj #{1..10} 0.01
meshop.weldVertSet $obj #{1,2,3}      -- weld to average position
meshop.deleteVerts $obj #{5..10}
meshop.deleteIsoVerts $obj            -- remove orphan verts
meshop.getIsoVerts $obj               -- BitArray of isolated verts
meshop.makeVertsPlanar $obj #{1..8}
meshop.moveVertsToPlane $obj #{1..n} [0,0,1] 0.0
```

**Performance**: Always pass vertlist as BitArray `#{}`, never Array `#()`.

## Editable_Mesh / TriMesh: Face Methods

```maxscript
getNumFaces $obj
getFace $obj 1                        -- returns Point3 of vert indices
setFace $obj 1 [1,2,3]               -- or: setFace $obj 1 1 2 3
deleteFace $obj 1                     -- delete single face
getFaceNormal $obj 1                  -- face normal as Point3
getFaceMatID $obj 1                   -- material ID (1-based)
setFaceMatID $obj 1 3                 -- set material ID
getFaceSmoothGroup $obj 1             -- 32-bit smoothing group integer
setFaceSmoothGroup $obj 1 1           -- set smoothing group

-- Selection
getFaceSelection $obj                 -- BitArray
setFaceSelection $obj #{1..10}

-- Extrude (Editable_Mesh only)
extrudeFace $obj #{1..16} 10 100 dir:#independent

-- meshop struct
meshop.deleteFaces $obj #{1..5} delIsoVerts:true
meshop.extrudeFaces $obj #{1} 5.0 -1.0 dir:#independent
meshop.bevelFaces $obj #{1..4} 5.0 -2.0 dir:#common
meshop.cloneFaces $obj #{1..10}
meshop.detachFaces $obj #{1..5} delete:true asMesh:true  -- returns TriMesh
meshop.collapseFaces $obj #{1..3}
meshop.divideFace $obj 1 baryCoord:[0.33,0.33,0.34]
meshop.divideFaces $obj #{1..n}       -- center subdivision
meshop.flipNormals $obj #{1..n}
meshop.unifyNormals $obj #{1..n}
meshop.autoSmooth $obj #{1..n} 45.0   -- threshold in degrees
meshop.getFaceArea $obj #{1}          -- area as float
meshop.getFaceCenter $obj 1
meshop.createPolygon $obj #(1,2,3,4) smGroup:1 matID:1
meshop.getFacesUsingVert $obj #{1}    -- faces touching vertex
meshop.getVertsUsingFace $obj #{1}    -- verts of face (BitArray, unordered!)
meshop.getElementsUsingFace $obj #{1} -- full connected element
meshop.getFacesByMatId $obj 2         -- faces with matID 2
```

## Editable_Mesh / TriMesh: Edge Methods

```maxscript
-- Edges: 3 per face. Edge index = (face-1)*3 + edgeInFace(1..3)
getEdgeVis $obj 1 1                   -- visibility of edge 1 on face 1
setEdgeVis $obj 1 1 true
getEdgeSelection $obj                 -- BitArray
setEdgeSelection $obj #{1,5,7}

-- meshop struct
meshop.chamferEdges $obj #{7,8} 5
meshop.extrudeEdges $obj #{5,7} 10.0 dir:#independent
meshop.collapseEdges $obj #{14,17}
meshop.divideEdge $obj 5 0.5          -- split at midpoint
meshop.divideEdges $obj #{1..4}       -- split all in half
meshop.turnEdge $obj 5                -- flip diagonal
meshop.getOpenEdges $obj              -- border edges (BitArray)
meshop.getEdgesUsingVert $obj #{1}
meshop.getVertsUsingEdge $obj #{1}
meshop.getFacesUsingEdge $obj #{1}
meshop.autoEdge $obj #{1..n} 30.0 type:#SetClear
```

## UV / Texture Mapping (Editable_Mesh)

```maxscript
-- Legacy Channel 1 methods
getNumTVerts $obj; setNumTVerts $obj 4
getTVert $obj 1; setTVert $obj 1 [0.0, 1.0, 0.0]  -- UVW as Point3
getTVFace $obj 1; setTVFace $obj 1 [1,2,3]
buildTVFaces $obj  -- MUST call after changing tvert count, before setting TVFaces

-- General mapping (all 100 channels, 0-based channel index)
meshop.getNumMaps $obj; meshop.setNumMaps $obj 3 keep:true
meshop.setMapSupport $obj 1 true
meshop.getNumMapVerts $obj 1; meshop.setNumMapVerts $obj 1 4
meshop.setMapVert $obj 1 1 [0,0,0]   -- channel, index, UVW
meshop.getMapVert $obj 1 1
meshop.setMapFace $obj 1 1 [1,2,3]   -- channel, face, vert indices
meshop.getMapFace $obj 1 1
meshop.defaultMapFaces $obj 1         -- auto 1:1 mapping
meshop.applyUVWMap $obj.mesh #planar channel:1
meshop.applyUVWMap $obj.mesh #box utile:2.0 vtile:2.0 channel:1

-- Vertex colors (channel 0)
meshop.setVertColor $obj 0 #{1..n} (color 255 0 0)
meshop.setFaceColor $obj 0 #{1..4} (color 0 255 0)
```

## Editable_Poly: polyop Methods

All polyop methods accept node or base object as first arg. Sub-object lists accept: `#all`, `#selection`, `#none`, BitArray, integer array, or single integer.

### Counts & Vertex Access

```maxscript
polyop.getNumVerts $obj
polyop.getNumEdges $obj
polyop.getNumFaces $obj
polyop.getVert $obj 1 node:$obj       -- in current coord system
polyop.setVert $obj #{1..5} [0,0,10]
polyop.moveVert $obj #{1..5} [0,0,10] useSoftSel:false
```

### Selection

```maxscript
polyop.getVertSelection $obj          -- BitArray
polyop.setVertSelection $obj #{1..5}
polyop.getEdgeSelection $obj
polyop.setEdgeSelection $obj #{1..10}
polyop.getFaceSelection $obj
polyop.setFaceSelection $obj #{1..4}
```

### Create

```maxscript
idx = polyop.createVert $obj [10,20,0]     -- returns new vert index
idx = polyop.createEdge $obj 1 5           -- between existing verts on same face
idx = polyop.createPolygon $obj #(1,2,5,4) -- returns face index
polyop.createShape $obj #{1..4} smooth:false name:"MyShape"
```

### Delete

```maxscript
polyop.deleteVerts $obj #{5..8}
polyop.deleteFaces $obj #{1..3} delIsoVerts:true
polyop.deleteEdges $obj #{1..2} delIsoVerts:true
polyop.deleteIsoVerts $obj
```

### Topology Queries (Get A Using B)

```maxscript
polyop.getEdgesUsingVert $obj #{1}    -- edges touching vert
polyop.getFacesUsingVert $obj #{1}    -- faces touching vert
polyop.getVertsUsingEdge $obj #{1}    -- verts of edge
polyop.getFacesUsingEdge $obj #{1}    -- faces sharing edge
polyop.getVertsUsingFace $obj #{1}    -- verts of face
polyop.getEdgesUsingFace $obj #{1}    -- edges of face
polyop.getElementsUsingFace $obj #{1} -- connected element
polyop.getVertsUsedOnlyByFaces $obj #{1..3}
```

### Face Properties

```maxscript
polyop.getFaceCenter $obj 1
polyop.getSafeFaceCenter $obj 1       -- better for non-convex faces
polyop.getFaceNormal $obj 1
polyop.getFaceArea $obj 1
polyop.getFaceMatID $obj 1
polyop.setFaceMatID $obj #{1..4} 3
```

### Extrude, Bevel, Chamfer

```maxscript
polyop.extrudeFaces $obj #{1..4} 10.0 -- uses .extrusionType property (0=Group,1=LocalNormal,2=ByPolygon)
polyop.bevelFaces $obj #{1..4} 10.0 -2.0  -- height, outline
polyop.chamferVerts $obj #{1..4} 2.0
polyop.chamferEdges $obj #{1..8} 1.5
```

### Collapse, Weld, Detach, Attach

```maxscript
polyop.collapseVerts $obj #{1..3}
polyop.collapseEdges $obj #{1..3}
polyop.collapseFaces $obj #{1..3}

polyop.weldVertsByThreshold $obj #{1..n}  -- uses .weldThreshold property
polyop.weldVerts $obj 1 2 [5,5,0]        -- weld 2 verts at location
polyop.weldEdgesByThreshold $obj #{1..4}

polyop.detachFaces $obj #{1..4} delete:true asNode:true name:"Detached"
polyop.attach $obj $otherNode             -- source node deleted after attach
```

### UV Mapping (polyop)

```maxscript
-- Channels are 0-based. 0=vertex color, 1=default UVW
polyop.getNumMaps $obj; polyop.setNumMaps $obj 3 keep:true
polyop.setMapSupport $obj 1 true
polyop.setNumMapVerts $obj 1 4
polyop.setMapVert $obj 1 1 [0,0,0]        -- channel, vert, UVW
polyop.getMapVert $obj 1 1
polyop.setMapFace $obj 1 1 #(1,2,3,4)     -- channel, face, vert array
polyop.getMapFace $obj 1 1                 -- returns array of indices
polyop.defaultMapFaces $obj 1
polyop.applyUVWMap $obj #box utile:1.0 vtile:1.0 channel:1
polyop.setVertColor $obj 0 #{1..n} (color 255 128 0)
```

## Edit_Poly Modifier

```maxscript
ep = Edit_Poly()
addModifier $obj ep
-- Most Edit_Poly methods need ep current in the Modify panel, or they fault
-- (EXCEPTION_ACCESS_VIOLATION) or silently do nothing (issue #47):
select $obj; max modify mode; modPanel.setCurrentObject ep

-- Selection via EditPolyMod interface (FAST standalone access)
EditPolyMod.SetEPolySelLevel ep #Face
EditPolyMod.GetSelection ep #Face           -- BitArray
sel = #{1..10}
EditPolyMod.SetSelection ep #Face &sel
ep.Select #Face #{1..10}                    -- alternative

-- Operations via ButtonOp
ep.ButtonOp #ExtrudeFace                    -- press extrude button
ep.ButtonOp #Bevel
ep.ButtonOp #ChamferEdge
ep.ButtonOp #ConnectEdges
ep.ButtonOp #Cap
ep.ButtonOp #MeshSmooth
ep.ButtonOp #SelectEdgeLoop
ep.ButtonOp #SelectEdgeRing
ep.Commit()                                 -- commit changes

-- Set operation properties BEFORE ButtonOp
ep.extrudeFaceHeight = 15.0
ep.extrudeFaceType = 1                      -- 0=Group, 1=LocalNormal, 2=ByPolygon
ep.edgeChamferAmount = 2.0
ep.edgeChamferSegments = 3
ep.connectEdgeSegments = 2
ep.connectEdgePinch = 0
ep.connectEdgeSlide = 0
ep.bevelHeight = 5.0; ep.bevelOutline = -1.0
ep.insetAmount = 2.0; ep.insetType = 1     -- 0=Group, 1=ByPolygon

-- Geometry data access
ep.GetNumVertices()
ep.GetVertex 1
ep.GetNumEdges()
ep.GetEdgeVertex 1 1                        -- edge, end(1or2)
ep.GetNumFaces()
ep.GetFaceDegree 1                          -- num sides
ep.GetFaceVertex 1 1                        -- face, corner
ep.GetFaceMaterial 1                        -- matID

-- Create geometry
ep.CreateVertex [10,0,0]
ep.CreateFace #(1,2,5,4)
ep.CreateEdge 1 5

-- Move/Rotate/Scale selection
ep.MoveSelection [0,0,10]
ep.RotateSelection (eulerAngles 0 0 45 as quat)
ep.ScaleSelection [1,1,0.5]

-- Slice
ep.SetSlicePlane [0,0,1] [0,0,25]          -- normal, center
ep.ButtonOp #Slice

-- Bridge
ep.bridgeSegments = 3; ep.bridgeTaper = 0.5
ep.BridgePolygons 1 6                       -- two face indices

-- Detach
name = "Piece01"
ep.DetachToObject &name
```

## Sub-Object Selection Patterns

```maxscript
-- Switch sub-object level
max modify mode
select $obj
subObjectLevel = 0  -- Object
subObjectLevel = 1  -- Vertex
subObjectLevel = 2  -- Edge
subObjectLevel = 3  -- Border (EPoly) / Face (EMesh)
subObjectLevel = 4  -- Face/Polygon
subObjectLevel = 5  -- Element

-- Editable_Mesh selection properties
$.selectedVerts    -- VertexSelection
$.selectedFaces    -- FaceSelection
$.selectedEdges    -- EdgeSelection

-- Grow/Shrink (Edit_Poly)
ep.ButtonOp #GrowSelection
ep.ButtonOp #ShrinkSelection

-- Convert selection between levels (Edit_Poly)
ep.ConvertSelection #Face #Edge             -- face sel -> edge sel
ep.ConvertSelectionToBorder #Face #Edge     -- border-only edges
```

## Common Patterns

```maxscript
-- Flatten sphere to plane
obj = sphere(); convertToMesh obj
for v = 1 to obj.numVerts do (
    p = getVert obj v; p.z = 0; setVert obj v p
)
update obj

-- Delete faces by normal direction
obj = geosphere(); convertToMesh obj
for v = obj.numVerts to 1 by -1 do
    if dot (getNormal obj v) [0,0,1] < -0.25 do deleteVert obj v
update obj

-- Extrude every face on EPoly
obj = convertToPoly (geosphere())
for f = polyop.getNumFaces obj to 1 by -1 do
    polyop.extrudeFaces obj #{f} 3.0

-- Build mesh manually with UV
m = mesh vertices:#([0,0,0],[10,0,0],[10,10,0],[0,10,0]) \
    faces:#([1,2,3],[1,3,4])
setNumTVerts m 4; buildTVFaces m
setTVert m 1 [0,0,0]; setTVert m 2 [1,0,0]
setTVert m 3 [1,1,0]; setTVert m 4 [0,1,0]
for i = 1 to m.numfaces do setTVFace m i (getFace m i)
update m

-- Select faces by material ID, convert to edge selection
polyop.setFaceSelection $obj (polyop.getFacesByFlag $obj 1 mask:0x1)
-- Or directly:
faces = meshop.getFacesByMatId $obj 2
setFaceSelection $obj faces; update $obj

-- Attach all selected objects to one
target = selection[1]; convertToPoly target
for i = selection.count to 2 by -1 do polyop.attach target selection[i]

-- Detach faces as new node
polyop.detachFaces $obj #{1..10} delete:true asNode:true name:"Split"

-- Ray intersection: snap point to mesh surface
r = ray [0,0,100] [0,0,-1]
hitResult = intersectRay $obj r  -- returns ray (pos + dir as normal) or undefined
if hitResult != undefined do sphere pos:hitResult.pos radius:0.5
```

## Boolean Operations (Editable_Mesh)

```maxscript
result = $meshA + $meshB   -- union (modifies $meshA)
result = $meshA - $meshB   -- difference
result = $meshA * $meshB   -- intersection
```

## Key Differences: Mesh vs Poly

| Feature | Editable_Mesh (meshop) | Editable_Poly (polyop) |
|---------|----------------------|----------------------|
| Face type | Triangles only | N-gon polygons |
| Struct | `meshop.*` | `polyop.*` |
| Must call update | YES | No (auto) |
| Edge count | 3 per face | Variable per face |
| Map face format | Point3 | Array of ints |
| Detach returns | TriMesh (asMesh:true) | bool (asNode:true creates node) |
| Border level | No | Yes (level 3) |
| Dead elements | No | Yes (call collapseDeadStructs) |
