import FreeCAD as App

print("FREECAD_VERSION=" + ".".join(App.Version()[:3]))
print("RESOURCE_DIR=" + App.getResourceDir())
print("APP_TYPE_APIS=" + ",".join(name for name in dir(App) if "type" in name.lower()))

try:
    import FreeCADGui as Gui
    workbenches = sorted(Gui.listWorkbenches())
    print("WORKBENCH_COUNT=" + str(len(workbenches)))
    print("WORKBENCH_SAMPLE=" + ",".join(workbenches[:8]))
except Exception as exc:
    print("GUI_QUERY_ERROR=" + repr(exc))

document = App.newDocument("AxisRuntimeProbe")
supported_types = sorted(document.supportedTypes())
print("SUPPORTED_TYPE_COUNT=" + str(len(supported_types)))
print("SUPPORTED_TYPE_SAMPLE=" + ",".join(supported_types[:12]))
print("IMPORT_TYPES=" + ",".join(sorted(App.getImportType().keys())[:12]))
print("EXPORT_TYPES=" + ",".join(sorted(App.getExportType().keys())[:12]))
obj = document.addObject("PartDesign::Feature", "ProbeFeature")
print("CREATED_TYPE=" + obj.TypeId)
print("PROPERTY_SAMPLE=" + ",".join(obj.PropertiesList[:8]))
App.closeDocument(document.Name)
print("MINIMAL_ACTION=ok")
