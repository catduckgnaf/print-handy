import os
import vtk
from vtk.util.colors import *
OUT=os.path.abspath(os.path.dirname(__file__))
parts=[('01_CENTER_TRAY_214x214.stl',(0,0,10),(0.18,0.45,0.72)),('02_RIGHT_2U_ARM.stl',(0,0,0),(0.23,0.25,0.3)),('03_LEFT_2U_ARM.stl',(0,0,0),(0.23,0.25,0.3))]
renderer=vtk.vtkRenderer()
renderer.SetBackground(0.94,0.95,0.97)
renderer.GradientBackgroundOn()
renderer.SetBackground2(0.81,0.85,0.91)
actors=[]
for fn,coord,rgb in parts:
  reader=vtk.vtkSTLReader();reader.SetFileName(os.path.join(OUT,fn));reader.Update()
  mapper=vtk.vtkPolyDataMapper();mapper.SetInputConnection(reader.GetOutputPort())
  actor=vtk.vtkActor();actor.SetMapper(mapper)
  actor.SetPosition(*coord)
  actor.GetProperty().SetColor(*rgb)
  actor.GetProperty().SetSpecular(0.22)
  actor.GetProperty().SetSpecularPower(15)
  actor.GetProperty().SetInterpolationToPhong()
  renderer.AddActor(actor); actors.append(actor)

# Stylized translucent Mac envelope (196.8x196.8x36): not a printable part
cube=vtk.vtkCubeSource()
cube.SetXLength(197);cube.SetYLength(197);cube.SetZLength(36);cube.SetCenter(0,111.5,38)
mapper=vtk.vtkPolyDataMapper();mapper.SetInputConnection(cube.GetOutputPort())
mac=vtk.vtkActor();mac.SetMapper(mapper)
mac.GetProperty().SetColor(0.72,0.75,0.78)
mac.GetProperty().SetOpacity(0.67)
mac.GetProperty().SetSpecular(0.65)
renderer.AddActor(mac)

# Rack rail reference behind flanges (ghosts)
for x in (-232.5,232.5):
  rc=vtk.vtkCubeSource();rc.SetXLength(18);rc.SetYLength(6);rc.SetZLength(90);rc.SetCenter(x,-8,44.5)
  m=vtk.vtkPolyDataMapper();m.SetInputConnection(rc.GetOutputPort())
  a=vtk.vtkActor();a.SetMapper(m);a.GetProperty().SetColor(0.55,0.57,0.60);a.GetProperty().SetOpacity(0.16)
  renderer.AddActor(a)
light=vtk.vtkLight();light.SetPosition(-400,-500,900);light.SetIntensity(1.0)
renderer.AddLight(light)
renWin=vtk.vtkRenderWindow();renWin.SetOffScreenRendering(1);renWin.AddRenderer(renderer);renWin.SetSize(1450,950)
renWin.SetMultiSamples(4)
cam=renderer.GetActiveCamera()
cam.SetFocalPoint(0,92,27);cam.SetPosition(590,-540,350);cam.SetViewUp(0,0,1)
cam.SetParallelProjection(True);cam.SetParallelScale(290)
renderer.ResetCameraClippingRange()
renWin.Render()
windowToImage=vtk.vtkWindowToImageFilter();windowToImage.SetInput(renWin);windowToImage.SetScale(1);windowToImage.Update()
writer=vtk.vtkPNGWriter();writer.SetInputConnection(windowToImage.GetOutputPort());writer.SetFileName(os.path.join(OUT,'PREVIEW_assembled_rack_mount.png'));writer.Write()
print('Wrote rendering',os.path.join(OUT,'PREVIEW_assembled_rack_mount.png'))
