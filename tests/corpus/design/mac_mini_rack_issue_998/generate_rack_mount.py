"""
Mac mini late 2014 2U 19-inch printable rack cradle v1.0.
Copyright 2026. Released CC0-1.0 (public domain).
Requires CadQuery 2.x. All units millimeters.

Coordinates: x across rack, y rearwards, z up, rack front face y=-26.
Assembly parts exported as individual STLs; use in CAD or slicer directly.
"""
import os, math
import cadquery as cq
from cadquery import Vector

OUT = os.path.abspath(os.path.dirname(__file__))
# Dimensions, mm
RACK_WIDTH = 482.6
RACK_HOLE_SPACING = 465.1
RACK_HOLE_X = RACK_HOLE_SPACING / 2
RACK_U = 44.45
TRAY_W, TRAY_D = 214.0, 214.0
MAC_W = 197.0
TRAY_T = 10.0
BOLT_XY = [(78,20),(99,20),(78,50),(99,50),(99,94),(99,149)]
# Rack hole center positions (2U, hole pattern in each U)
EAR_HOLES_Z = (6.35,22.225,66.675,82.55)


def rect(x0,x1,y0,y1,z0,z1):
    return cq.Workplane('XY').box(x1-x0,y1-y0,z1-z0,centered=(False,False,False)).translate((x0,y0,z0))

def solid_union(items):
    acc=items[0]
    for p in items[1:]:
        acc=acc.union(p)
    return acc

def cylinder_z(x,y,z0,z1,r):
    return cq.Workplane('XY').circle(r).extrude(z1-z0).translate((x,y,z0))

def make_tray():
    # Bottom perimeter, under-foot crossbars and central rib. Large holes keep intake clear.
    shapes=[
        rect(-107,-88,0,214,0,10), rect(88,107,0,214,0,10),
        rect(-107,107,0,12,0,10), rect(-107,107,206,214,0,10),
        rect(-88,88,38,50,0,10), rect(-88,88,171,183,0,10),
        rect(-6,6,49,172,0,10),
        # Front bolt splice pads
        rect(-107,-68,0,61,0,10),rect(68,107,0,61,0,10),
        # rounded-corner-friendly side retaining lips with ~2.5 mm slack
        rect(-107,-101,13,207,10,24),rect(101,107,13,207,10,24),
        # forward stops at side corners, rear stop at side corners
        rect(-107,-101,8,13,10,20),rect(101,107,8,13,10,20),
        rect(-107,-101,207,214,10,20),rect(101,107,207,214,10,20),
    ]
    t=solid_union(shapes)
    # Four M4 low-button-head recesses per front splice (both sides) and deeper outrigger holes.
    # Main bolt shank diameter 4.6 mm, c'bore 8.8 diameter x3.5 deep from top (z10).
    for sign in (-1,1):
        for bx,by in BOLT_XY:
            x=bx*sign
            t=t.cut(cylinder_z(x,by,-0.5,10.5,2.30))
            t=t.cut(cylinder_z(x,by,6.5,10.5,4.40))
        # slotted strap eyelets through the bottom side rail, avoid fastener holes
        # 20mm velcro strap, slot 7 x 27 mm. Longitudinal rail is still continuous on both sides.
        for sy in (116,180):
            # right: x 91..98. left: -98..-91
            x0,x1=(91,98) if sign==1 else (-98,-91)
            t=t.cut(rect(x0,x1,sy-13.5,sy+13.5,-0.5,10.5))
    return t


def ear_rack_slot(x,z):
    # Slots 9.8mm wide x 7.8mm tall accommodate mounting tolerance and M6 hardware.
    # Axis through face (Y).
    r=3.9; dx=1.0
    c1=cq.Solid.makeCylinder(r,20,Vector(x-dx,-29,z),Vector(0,1,0))
    c2=cq.Solid.makeCylinder(r,20,Vector(x+dx,-29,z),Vector(0,1,0))
    bridge=rect(x-dx,x+dx,-29,-9,z-r,z+r)
    return cq.Workplane(obj=c1).union(cq.Workplane(obj=c2)).union(bridge)


def make_right_arm():
    # Mechanically coupled triangular side gusset, a continuous rib and a wide
    # bolted lap under the Mac mini. Six M4 bolts share moment & shear per side.
    main=[
        # under-tray lap pads; front four bolts + two more deeper into chassis
        rect(69,124,0,62,0,10),
        rect(89,124,59,162,0,10),
        # very strong deep outer side spine
        rect(108,123,0,161,0,28),
        # front 'C' section horizontal rack beam
        rect(108,225,-22,-14,18,75),  # web
        rect(108,225,-22,1,0,27),    # bottom flange
        rect(110,225,-22,-9,66,75),   # top return flange
    ]
    arm=solid_union(main)
    # Thick 10 mm tall triangular gusset gradually tapering toward back of tray.
    poly=[(-22,0),(-22,75),(55,31),(161,28),(161,0)]
    gusset=cq.Workplane('YZ').polyline(poly).close().extrude(10).translate((109,0,0))
    arm=arm.union(gusset)
    # Additional diagonal braces at outer ear and inside arm mitigate twisting
    # of the channel section while leaving plenty of room for cables.
    brace=cq.Workplane('XZ').polyline([(209,0),(220,0),(220,80),(209,68)]).close().extrude(12).translate((0,-9,0))
    # XZ plane has normal -Y. brace extrudes y -9..-21, entirely within channel/ear.
    arm=arm.union(brace)
    # Both ear and arms stay completely within the 2U envelope. M4 hex nuts
    # are captured flush inside the plate; no hardware protrudes below z=0.
    arm=arm.union(rect(220,241.3,-26,-12,0.0,88.5))
    for bx,by in BOLT_XY:
        arm=arm.cut(cylinder_z(bx,by,-0.5,10.5,2.30))
        nut=cq.Workplane('XY').center(bx,by).polygon(6,8.45).extrude(4.1)
        arm=arm.cut(nut)
    for z in EAR_HOLES_Z:
        arm=arm.cut(ear_rack_slot(RACK_HOLE_X,z))
    return arm


def make_left_arm(right):
    return right.mirror('YZ',union=False)


def output(name,model):
    print('Exporting',name,flush=True)
    # STL high-enough tessellation for equipment; no unnecessary massive mesh.
    cq.exporters.export(model,os.path.join(OUT,name+'.stl'),tolerance=0.13,angularTolerance=0.18)
    cq.exporters.export(model,os.path.join(OUT,name+'.step'))
    return model

if __name__=='__main__':
    print('Building tray',flush=True)
    tray=make_tray()
    print('Building arms',flush=True)
    armR=make_right_arm()
    armL=make_left_arm(armR)
    output('01_CENTER_TRAY_214x214',tray)
    output('02_RIGHT_2U_ARM',armR)
    output('03_LEFT_2U_ARM',armL)
    # STEP assembly preserved in rack coordinate frame.
    assembly=cq.Assembly(name='2014_Mac_mini_19in_2U_v1')
    assembly.add(tray.translate((0,0,10)),name='vented_center_tray',color=cq.Color(0.14,0.33,0.58))
    assembly.add(armR,name='right_rack_arm',color=cq.Color(0.18,0.18,0.20))
    assembly.add(armL,name='left_rack_arm',color=cq.Color(0.18,0.18,0.20))
    assembly.save(os.path.join(OUT,'ASSEMBLY_2014_MAC_MINI_2U.step'))
    # Also export simple ghost Mac envelope STEP for fit visualization.
    envelope=rect(-98.5,98.5,13,210,20,56)
    cq.exporters.export(envelope,os.path.join(OUT,'REFERENCE_MAC_MINI_ENVELOPE_NOT_FOR_PRINTING.step'))
    # Two tiny calibration coupons. Print first to verify captive M4 nut fit and
    # chosen screw head/length before committing to the full-size parts.
    test_bottom=rect(0,24,0,24,0,10)
    test_bottom=test_bottom.cut(cylinder_z(12,12,-0.5,10.5,2.30))
    test_bottom=test_bottom.cut(cq.Workplane('XY').center(12,12).polygon(6,8.45).extrude(4.1))
    test_top=rect(0,24,0,24,0,10)
    test_top=test_top.cut(cylinder_z(12,12,-0.5,10.5,2.30))
    test_top=test_top.cut(cylinder_z(12,12,6.5,10.5,4.40))
    cq.exporters.export(test_bottom,os.path.join(OUT,'00_TEST_LOWER_M4_NUT.stl'))
    cq.exporters.export(test_top,os.path.join(OUT,'00_TEST_UPPER_M4_BUTTON_HEAD.stl'))
    # Make a bill of materials and summary of key geometry.
    for name, shape in [('tray',tray),('R arm',armR),('L arm',armL)]:
        s=shape.val(); b=s.BoundingBox()
        print(name,'solids',len(shape.solids().vals()),'valid',s.isValid(),
              'size',round(b.xlen,2),round(b.ylen,2),round(b.zlen,2),
              'volume cc',round(s.Volume()/1000,1),flush=True)
    print('Output folder:',OUT,flush=True)

