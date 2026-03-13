"""
/***************************************************************************
 Serval,  a QGIS plugin for manipulating raster cell values

    begin            : 2015-12-30
    copyright        : (C) 2020 Radosław Pasiok for Lutra Consulting Ltd.
    email            : info@lutraconsulting.co.uk
 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/
"""

import math
import os.path
from datetime import datetime, timedelta

from qgis.PyQt.QtCore import QSize, Qt, QUrl, QVariant, QSettings
from qgis.PyQt.QtGui import QPixmap, QCursor, QIcon, QColor, QDesktopServices
from qgis.PyQt.QtWidgets import (
    QAction,
    QApplication,
    QComboBox,
    QInputDialog,
    QLabel,
    QLineEdit,
)
from qgis.core import (
    QgsCoordinateTransform,
    QgsCsException,
    QgsExpression,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsMapLayerType,
    QgsMeshDatasetIndex,
    QgsPointXY,
    QgsProject,
    QgsRaster,
    QgsRectangle,
    QgsSpatialIndex,
    QgsVectorLayer,
)
from qgis.gui import (QgsDoubleSpinBox, QgsMapToolEmitPoint, QgsColorButton, QgsExpressionBuilderDialog, )

from .raster_handler import RasterHandler
from .selection_tool import RasterCellSelectionMapTool
from .serval_exp_functions import (
    interpolate_from_mesh,
    intersecting_features_attr_average,
    nearest_feature_attr_value,
    nearest_pt_on_line_interpolate_z,
)
from .band_spin_boxes import BandBoxes
from .layer_select_dlg import LayerSelectDialog
from .raster_changes import RasterChanges
from .utils import is_number, icon_path, dtypes, get_logger, check_gdal_driver_create_option
from .user_communication import UserCommunication

DEBUG = False


class Serval(object):

    LINE_SELECTION = "line"
    POLYGON_SELECTION = "polygon"
    RGB = "RGB"
    SINGLE_BAND = "Single band"

    def __init__(self, iface):
        self.iface = iface
        self.canvas = self.iface.mapCanvas()
        self.plugin_dir = os.path.dirname(__file__)
        self.uc = UserCommunication(iface, 'Serval')
        self.load_settings()
        self.raster = None
        self.handler = None
        self.spin_boxes = None
        self.exp_dlg = None
        self.exp_builder = None
        self.block_pts_layer = None
        self.px, self.py = [0, 0]
        self.last_point = QgsPointXY(0, 0)
        self.rbounds = None
        self.changes = dict()  # dict with rasters changes {raster_id: RasterChanges instance}
        self.project = QgsProject.instance()
        self.crs_transform = None
        self.all_touched = None
        self.selection_mode = None
        self.spatial_index_time = {}  # {layer_id: creation time}
        self.spatial_index = {}       # {layer_id: QgsSpatialIndex}
        self.selection_layers_count = 1
        self.debug = DEBUG
        self.logger = get_logger() if self.debug else None

        self.menu = u'Serval'
        self.actions = []
        self.actions_always_on = []
        self.toolbar = self.iface.addToolBar(u'Serval Main Toolbar')
        self.toolbar.setObjectName(u'Serval Main Toolbar')
        self.toolbar.setToolTip(u'Serval Main Toolbar')

        self.sel_toolbar = self.iface.addToolBar(u'Serval Selection Toolbar')
        self.sel_toolbar.setObjectName(u'Serval Selection Toolbar')
        self.sel_toolbar.setToolTip(u'Serval Selection Toolbar')

        # Map tools
        self.probe_tool = QgsMapToolEmitPoint(self.canvas)
        self.probe_tool.setObjectName('ServalProbeTool')
        self.probe_tool.setCursor(QCursor(QPixmap(icon_path('probe_tool.svg')), hotX=2, hotY=22))
        self.probe_tool.canvasClicked.connect(self.point_clicked)
        self.draw_tool = QgsMapToolEmitPoint(self.canvas)
        self.draw_tool.setObjectName('ServalDrawTool')
        self.draw_tool.setCursor(QCursor(QPixmap(icon_path('draw_tool.svg')), hotX=2, hotY=22))
        self.draw_tool.canvasClicked.connect(self.point_clicked)
        self.selection_tool = RasterCellSelectionMapTool(self.iface, self.uc, self.raster, debug=self.debug)
        self.selection_tool.setObjectName('RasterSelectionTool')
        self.map_tool_btn = dict()  # {map tool: button activating the tool}

        self.iface.currentLayerChanged.connect(self.set_active_raster)
        if self.project is not None:
            self.project.layersAdded.connect(self.set_active_raster)
        self.canvas.mapToolSet.connect(self.check_active_tool)

        self.register_exp_functions()

    def load_settings(self):
        """Return plugin settings dict - default values are overriden by user prefered values from QSettings."""
        self.default_settings = {
            "undo_steps": {"value": 3, "vtype": int},
        }
        self.settings = dict()
        s = QSettings()
        s.beginGroup("serval")
        for k, v in self.default_settings.items():
            user_val = s.value(k, v["value"], v["vtype"])
            self.settings[k] = user_val

    def edit_settings(self):
        """Open dialog with plugin settings."""
        s = QSettings()
        s.beginGroup("serval")
        k = "undo_steps"
        cur_val = self.settings[k]
        val_type = self.default_settings[k]["vtype"]
        cur_steps = s.value(k, cur_val, val_type)

        label = 'Nr of Undo/Redo steps:'
        steps, ok = QInputDialog.getInt(None, "Serval Settings", label, cur_steps)
        if not ok:
            return
        if steps >= 0:
            s.setValue("undo_steps", steps)
        self.load_settings()
        self.uc.show_info("Some new settings may require QGIS restart.")

    def initGui(self):
        _ = self.add_action(
            'serval_icon.svg',
            text=u'Show Serval Toolbars',
            add_to_menu=True,
            callback=self.show_toolbar,
            always_on=True, )

        _ = self.add_action(
            'serval_icon.svg',
            text=u'Hide Serval Toolbars',
            add_to_menu=True,
            callback=self.hide_toolbar,
            always_on=True, )

        self.probe_btn = self.add_action(
            'probe.svg',
            text="Probe raster",
            callback=self.activate_probing,
            add_to_toolbar=self.toolbar,
            checkable=True, )
        self.map_tool_btn[self.probe_tool] = self.probe_btn

        self.color_btn = QgsColorButton()
        self.color_btn.setColor(QColor(Qt.GlobalColor.gray))
        self.color_btn.setMinimumSize(QSize(40, 24))
        self.color_btn.setMaximumSize(QSize(40, 24))
        self.toolbar.addWidget(self.color_btn)
        self.color_picker_connection(connect=True)
        self.color_btn.setDisabled(True)

        self.toolbar.addWidget(QLabel("Band:"))
        self.bands_cbo = QComboBox()
        self.bands_cbo.addItem("1", 1)
        self.toolbar.addWidget(self.bands_cbo)
        self.bands_cbo.currentIndexChanged.connect(self.update_active_bands)
        self.bands_cbo.setDisabled(True)

        self.spin_boxes = BandBoxes()
        self.toolbar.addWidget(self.spin_boxes)
        self.spin_boxes.enter_hit.connect(self.apply_spin_box_values)

        self.draw_btn = self.add_action(
            'draw.svg',
            text="Apply Value(s) To Single Cell",
            callback=self.activate_drawing,
            add_to_toolbar=self.toolbar,
            checkable=True, )
        self.map_tool_btn[self.draw_tool] = self.draw_btn

        self.apply_spin_box_values_btn = self.add_action(
            'apply_const_value.svg',
            text="Apply Value(s) to Selection",
            callback=self.apply_spin_box_values,
            add_to_toolbar=self.toolbar, )

        self.gom_btn = self.add_action(
            'apply_nodata_value.svg',
            text="Apply NoData to Selection",
            callback=self.apply_nodata_value,
            add_to_toolbar=self.toolbar, )

        self.exp_dlg_btn = self.add_action(
            'apply_expression_value.svg',
            text="Apply Expression Value To Selection",
            callback=self.define_expression,
            add_to_toolbar=self.toolbar,
            checkable=False, )

        self.low_pass_filter_btn = self.add_action(
            'apply_low_pass_filter.svg',
            text="Apply Low-Pass 3x3 Filter To Selection",
            callback=self.apply_low_pass_filter,
            add_to_toolbar=self.toolbar,
            checkable=False, )

        self.undo_btn = self.add_action(
            'undo.svg',
            text="Undo",
            callback=self.undo,
            add_to_toolbar=self.toolbar, )

        self.redo_btn = self.add_action(
            'redo.svg',
            text="Redo",
            callback=self.redo,
            add_to_toolbar=self.toolbar, )

        self.set_nodata_btn = self.add_action(
            'set_nodata.svg',
            text="Edit Raster NoData Values",
            callback=self.set_nodata,
            add_to_toolbar=self.toolbar, )

        self.settings_btn = self.add_action(
            'edit_settings.svg',
            text="Serval Settings",
            callback=self.edit_settings,
            add_to_toolbar=self.toolbar,
            always_on=True, )

        self.show_help = self.add_action(
            'help.svg',
            text="Help",
            add_to_menu=True,
            callback=self.show_website,
            add_to_toolbar=self.toolbar,
            always_on=True, )

        # Selection Toolbar

        line_width_icon = QIcon(icon_path("line_width.svg"))
        line_width_lab = QLabel()
        line_width_lab.setPixmap(line_width_icon.pixmap(22, 12))
        self.sel_toolbar.addWidget(line_width_lab)

        self.line_width_sbox = QgsDoubleSpinBox()
        self.line_width_sbox.setMinimumSize(QSize(50, 24))
        self.line_width_sbox.setMaximumSize(QSize(50, 24))
        # self.line_width_sbox.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.line_width_sbox.setValue(1)
        self.line_width_sbox.setMinimum(0.01)
        self.line_width_sbox.setShowClearButton(False)
        self.line_width_sbox.setToolTip("Selection Line Width")
        self.line_width_sbox.valueChanged.connect(self.update_selection_tool)

        self.width_unit_cbo = QComboBox()
        self.width_units = ("map units", "pixel width", "pixel height", "hairline",)
        for u in self.width_units:
            self.width_unit_cbo.addItem(u)
        self.width_unit_cbo.setToolTip("Selection Line Width Unit")
        self.sel_toolbar.addWidget(self.line_width_sbox)
        self.sel_toolbar.addWidget(self.width_unit_cbo)
        self.width_unit_cbo.currentIndexChanged.connect(self.update_selection_tool)

        self.line_select_btn = self.add_action(
            'select_line.svg',
            text="Select Raster Cells by Line",
            callback=self.activate_line_selection,
            add_to_toolbar=self.sel_toolbar,
            checkable=True, )

        self.polygon_select_btn = self.add_action(
            'select_polygon.svg',
            text="Select Raster Cells by Polygon",
            callback=self.activate_polygon_selection,
            add_to_toolbar=self.sel_toolbar,
            checkable=True, )

        self.selection_from_layer_btn = self.add_action(
            'select_from_layer.svg',
            text="Create Selection From Layer",
            callback=self.selection_from_layer,
            add_to_toolbar=self.sel_toolbar, )

        self.selection_to_layer_btn = self.add_action(
            'selection_to_layer.svg',
            text="Create Memory Layer From Selection",
            callback=self.selection_to_layer,
            add_to_toolbar=self.sel_toolbar, )

        self.clear_selection_btn = self.add_action(
            'clear_selection.svg',
            text="Clear selection",
            callback=self.clear_selection,
            add_to_toolbar=self.sel_toolbar, )

        self.toggle_all_touched_btn = self.add_action(
            'all_touched.svg',
            text="Toggle All Touched Get Selected",
            callback=self.toggle_all_touched,
            checkable=True, checked=True,
            add_to_toolbar=self.sel_toolbar, )
        self.all_touched = True

        self.enable_toolbar_actions(enable=False)
        self.check_undo_redo_btns()

    def add_action(self, icon_name, callback=None, text="", enabled_flag=True, add_to_menu=False, add_to_toolbar=None,
                   status_tip=None, whats_this=None, checkable=False, checked=False, always_on=False):
            
        icon = QIcon(icon_path(icon_name))
        action = QAction(icon, text, self.iface.mainWindow())
        action.triggered.connect(callback)
        action.setEnabled(enabled_flag)
        action.setCheckable(checkable)
        action.setChecked(checked)

        if status_tip is not None:
            action.setStatusTip(status_tip)
        if whats_this is not None:
            action.setWhatsThis(whats_this)
        if add_to_toolbar is not None:
            add_to_toolbar.addAction(action)
        if add_to_menu:
            self.iface.addPluginToMenu(self.menu, action)

        self.actions.append(action)
        if always_on:
            self.actions_always_on.append(action)
        return action

    def unload(self):
        self.changes.clear()
        if self.selection_tool:
            self.selection_tool.reset()
        if self.spin_boxes is not None:
            self.spin_boxes.remove_spinboxes()
        for action in self.actions:
            self.iface.removePluginMenu('Serval', action)
            self.iface.removeToolBarIcon(action)
        del self.toolbar
        del self.sel_toolbar
        self.iface.actionPan().trigger()
        self.unregister_exp_functions()

    def show_toolbar(self):
        if self.toolbar:
            self.toolbar.show()
            self.sel_toolbar.show()

    def hide_toolbar(self):
        if self.toolbar:
            self.toolbar.hide()
            self.sel_toolbar.hide()

    @staticmethod
    def register_exp_functions():
        QgsExpression.registerFunction(nearest_feature_attr_value)
        QgsExpression.registerFunction(nearest_pt_on_line_interpolate_z)
        QgsExpression.registerFunction(intersecting_features_attr_average)
        QgsExpression.registerFunction(interpolate_from_mesh)

    @staticmethod
    def unregister_exp_functions():
        QgsExpression.unregisterFunction('nearest_feature_attr_value')
        QgsExpression.unregisterFunction('nearest_pt_on_line_interpolate_z')
        QgsExpression.unregisterFunction('intersecting_features_attr_average')
        QgsExpression.unregisterFunction('interpolate_from_mesh')

    def uncheck_all_btns(self):
        self.probe_btn.setChecked(False)
        self.draw_btn.setChecked(False)
        self.gom_btn.setChecked(False)
        self.line_select_btn.setChecked(False)
        self.polygon_select_btn.setChecked(False)

    def check_active_tool(self, cur_tool):
        self.uncheck_all_btns()
        if cur_tool in self.map_tool_btn:
            self.map_tool_btn[cur_tool].setChecked(True)
        if cur_tool == self.selection_tool:
            if self.selection_mode == self.LINE_SELECTION:
                self.line_select_btn.setChecked(True)
            else:
                self.polygon_select_btn.setChecked(True)

    def activate_probing(self):
        self.mode = 'probe'
        self.canvas.setMapTool(self.probe_tool)

    def define_expression(self):
        if not self.selection_tool.selected_geometries:
            self.uc.bar_warn("No selection for raster layer. Select some cells and retry...")
            return
        handler = self.handler
        if handler is None:
            if self.logger:
                self.logger.warning("Raster handler is not available")
            return
        all_touched = bool(self.all_touched)
        handler.select(self.selection_tool.selected_geometries, all_touched_cells=all_touched)
        handler.create_cell_pts_layer()

        cell_pts_layer = handler.cell_pts_layer
        if cell_pts_layer is None or cell_pts_layer.featureCount() == 0:
            self.uc.bar_warn("No selection for raster layer. Select some cells and retry...")
            return
        self.exp_dlg = QgsExpressionBuilderDialog(cell_pts_layer)
        self.exp_builder = self.exp_dlg.expressionBuilder()
        self.exp_dlg.accepted.connect(self.apply_exp_value)
        self.exp_dlg.show()

    def apply_exp_value(self):
        exp_dlg = self.exp_dlg
        exp_builder = self.exp_builder
        handler = self.handler
        raster = self.raster

        if exp_dlg is None or exp_builder is None or handler is None or raster is None:
            if self.logger:
                self.logger.warning("Expression dialog/builder, raster handler, or raster layer is not available")
            return

        if not exp_dlg.expressionText() or not exp_builder.isExpressionValid():
            return
        
        cell_pts_layer = handler.cell_pts_layer
        if cell_pts_layer is None:
            if self.logger:
                self.logger.warning("Cell points layer is not available")
            return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            exp = exp_dlg.expressionText()
            idx = cell_pts_layer.addExpressionField(exp, QgsField('exp_val', QVariant.Double))
            handler.exp_field_idx = idx
            handler.write_block()
        finally:
            QApplication.restoreOverrideCursor()

        raster.triggerRepaint()

    def activate_drawing(self):
        self.mode = 'draw'
        self.canvas.setMapTool(self.draw_tool)

    def get_cur_line_width(self):
        raster = self.raster
        if raster is None:
            if self.logger:
                self.logger.warning("Active raster layer is not available")
            return self.line_width_sbox.value()
        width_coef = {
            "map units": 1.0,
            "pixel width": raster.rasterUnitsPerPixelX(),
            "pixel height": raster.rasterUnitsPerPixelY(),
            "hairline": 0.000001,
        }
        return self.line_width_sbox.value() * width_coef[self.width_unit_cbo.currentText()]

    def set_selection_tool(self, mode):
        if self.raster is None:
            self.uc.bar_warn("Select a raster layer")
            return
        self.selection_mode = mode
        self.selection_tool.init_tool(self.raster, mode=self.selection_mode, line_width=self.get_cur_line_width())
        self.selection_tool.set_prev_tool(self.canvas.mapTool())
        self.canvas.setMapTool(self.selection_tool)

    def activate_line_selection(self):
        self.set_selection_tool(self.LINE_SELECTION)

    def activate_polygon_selection(self):
        self.set_selection_tool(self.POLYGON_SELECTION)

    def update_selection_tool(self):
        """Reactivate the selection tool with updated line width and units."""
        if self.selection_mode == self.LINE_SELECTION:
            self.activate_line_selection()
        elif self.selection_mode == self.POLYGON_SELECTION:
            self.activate_polygon_selection()
        else:
            pass

    def apply_values(self, new_values):
        handler = self.handler
        raster = self.raster
        if handler is None or raster is None:
            if self.logger:
                self.logger.warning("Raster handler or active raster layer is not available")
            return
        all_touched = bool(self.all_touched)
        
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            handler.select(self.selection_tool.selected_geometries, all_touched_cells=all_touched)
            handler.write_block(new_values)
        finally:
            QApplication.restoreOverrideCursor()
        raster.triggerRepaint()

    def apply_values_single_cell(self, new_vals):
        """Create single cell selection and apply the new values."""
        handler = self.handler
        raster = self.raster
        cp = self.last_point
        if handler is None or raster is None:
            if self.logger:
                self.logger.warning("Raster handler or active raster layer is not available")
            return

        if self.logger:
            self.logger.debug(f"Changing single cell for pt {cp}")
        col, row = handler.point_to_index([cp.x(), cp.y()])
        px, py = handler.index_to_point(row, col, upper_left=False)
        d = 0.001
        bbox = QgsRectangle(px - d, py - d, px + d, py + d)
        if self.logger:
            self.logger.debug(f"Changing single cell in {bbox}")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            handler.select([QgsGeometry.fromRect(bbox)], all_touched_cells=False, transform=False)
            handler.write_block(new_vals)
        finally:        
            QApplication.restoreOverrideCursor()
        raster.triggerRepaint()

    def apply_spin_box_values(self):
        if not self.selection_tool.selected_geometries:
            return
        spin_boxes = self.spin_boxes
        if spin_boxes is None:
            if self.logger:
                self.logger.warning("Band spin boxes are not available")
            return
        self.apply_values(spin_boxes.get_values())

    def apply_nodata_value(self):
        if not self.selection_tool.selected_geometries:
            return
        handler = self.handler
        if handler is None:
            if self.logger:
                self.logger.warning("Raster handler is not available")
            return
        self.apply_values(handler.nodata_values)

    def apply_low_pass_filter(self):
        handler = self.handler
        raster = self.raster
        if handler is None or raster is None:
            if self.logger:
                self.logger.warning("Raster handler or active raster layer in not available")
            return
        all_touched = bool(self.all_touched)

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            handler.select(self.selection_tool.selected_geometries, all_touched_cells=all_touched)
            handler.write_block(low_pass_filter=True)
        finally:
            QApplication.restoreOverrideCursor()
        raster.triggerRepaint()

    def clear_selection(self):
        if self.selection_tool:
            self.selection_tool.clear_all_selections()

    def selection_from_layer(self):
        """Create a new selection from layer."""
        raster = self.raster
        if raster is None:
            if self.logger:
                self.logger.warning("Active raster layer is not available")
            return
        self.selection_tool.init_tool(raster, mode=self.POLYGON_SELECTION, line_width=self.get_cur_line_width())
        dlg = LayerSelectDialog()
        if not dlg.exec():
            return
        cur_layer = dlg.cbo.currentLayer()
        if cur_layer is None:
            if self.logger:
                self.logger.warning("No layer selected in layer selection dialog")
            return
        if not cur_layer.type() == QgsMapLayerType.VectorLayer:
            return
        self.selection_tool.selection_from_layer(cur_layer)

    def selection_to_layer(self):
        """Create a memory layer from current selection"""
        geoms = self.selection_tool.selected_geometries
        raster = self.raster
        project = self.project        
        
        if geoms is None or raster is None or project is None:
            if self.logger:
                self.logger.warning("Selection, raster, or project is not available")
            return
        crs_str = raster.crs().toProj()
        nr = self.selection_layers_count
        self.selection_layers_count += 1
        
        mlayer = QgsVectorLayer(f"Polygon?crs={crs_str}&field=fid:int", f"Raster selection {nr}", "memory")
        provider = mlayer.dataProvider()
        if provider is None:
            if self.logger:
                self.logger.warning("Memory layer data provider is not available")
            return        
        fields = provider.fields()        
        features = []
        for i, geom in enumerate(geoms):
            feat = QgsFeature(fields)
            feat["fid"] = i + 1
            feat.setGeometry(geom)
            features.append(feat)
        provider.addFeatures(features)
        project.addMapLayer(mlayer)

    def toggle_all_touched(self):
        """Toggle selection mode."""
        # button is toggled automatically when clicked, just update the attribute
        self.all_touched = self.toggle_all_touched_btn.isChecked()

    def point_clicked(self, point=None, button=None):
        raster = self.raster
        handler = self.handler
        spin_boxes = self.spin_boxes
        rbounds = self.rbounds
        
        if raster is None:
            self.uc.bar_warn("Choose a raster to work with...", dur=3)
            return

        if handler is None or spin_boxes is None or rbounds is None:
            if self.logger:
                self.logger.warning("Raster handler, band spin boxes, or raster bounds are not available")
            return

        if self.logger:
            self.logger.debug(f"Clicked point in canvas CRS: {point if point else self.last_point}")

        if point is None:
            ptxy_in_src_crs = self.last_point
        else:
            if self.crs_transform:
                if self.logger:
                    self.logger.debug(f"Transforming clicked point {point}")
                try:
                    ptxy_in_src_crs = self.crs_transform.transform(point)
                except QgsCsException as err:
                    self.uc.show_warn(
                        "Point coordinates transformation failed! Check the raster projection:\n\n{}".format(repr(err)))
                    return
            else:
                ptxy_in_src_crs = QgsPointXY(point.x(), point.y())

        if self.logger:
            self.logger.debug(f"Clicked point in raster CRS: {ptxy_in_src_crs}")
        self.last_point = ptxy_in_src_crs

        # check if the point is within active raster extent
        if not rbounds[0] <= ptxy_in_src_crs.x() <= rbounds[2]:
            self.uc.bar_info("Out of x bounds", dur=3)
            return
        if not rbounds[1] <= ptxy_in_src_crs.y() <= rbounds[3]:
            self.uc.bar_info("Out of y bounds", dur=3)
            return
        
        ident_vals = handler.provider.identify(ptxy_in_src_crs, QgsRaster.IdentifyFormatValue).results()
        cur_vals = list(ident_vals.values())

        if self.mode == 'draw':
            new_vals = spin_boxes.get_values()
            if self.logger:
                self.logger.debug(f"Applying const value {new_vals}")
            self.apply_values_single_cell(new_vals)
        else:
            spin_boxes.set_values(cur_vals)
            if 2 < handler.bands_nr < 5:
                self.color_picker_connection(connect=False)
                self.color_btn.setColor(QColor(*spin_boxes.get_values()[:4]))
                self.color_picker_connection(connect=True)

    def set_values_from_picker(self, c):
        """Set bands spinboxes values after color change in the color picker"""
        handler = self.handler
        spin_boxes = self.spin_boxes
        
        if handler is None or spin_boxes is None:
            if self.logger:
                self.logger.warning("Raster handler or band spin boxes are not available")
            return
        
        values = None
        if handler.bands_nr > 2:
            values = [c.red(), c.green(), c.blue()]
            if handler.bands_nr == 4:
                values.append(c.alpha())
        if values:
            spin_boxes.set_values(values)

    def set_nodata(self):
        """Set NoData value(s) for each band of current raster."""
        raster = self.raster
        handler = self.handler

        if raster is None:
            self.uc.bar_warn('Select a raster layer to define/change NoData value!')
            return
        if handler is None:
            if self.logger:
                self.logger.warning("Raster handler is not available")
            return

        provider = handler.provider

        if provider.userNoDataValues(1):
            note = '\nNote: there is a user defined NODATA value.\nCheck the raster properties (Transparency).'
        else:
            note = ''
        dt = provider.dataType(1)

        # current NODATA value
        if provider.sourceHasNoDataValue(1):
            cur_nodata = provider.sourceNoDataValue(1)
            if dt < 6:
                cur_nodata = '{0:d}'.format(int(float(cur_nodata)))
        else:
            cur_nodata = ''
        
        label = 'Define/change raster NODATA value.\n\n'
        label += 'Raster src_data type: {}.{}'.format(dtypes[dt]['name'], note)
        nd, ok = QInputDialog.getText(None, "Define NODATA Value", label, QLineEdit.EchoMode.Normal, str(cur_nodata))
        if not ok:
            return
        if not is_number(nd):
            self.uc.show_warn('Wrong NODATA value!')
            return
        new_nodata = int(nd) if dt < 6 else float(nd)
        
        # set the NODATA value for each band
        res = []
        for nr in handler.bands_range:
            res.append(provider.setNoDataValue(nr, new_nodata))
            provider.sourceHasNoDataValue(nr)
        
        if False in res:
            self.uc.show_warn('Setting new NODATA value failed!')
        else:
            self.uc.bar_info('Successful setting new NODATA values!', dur=2)

        self.set_active_raster()
        if self.raster is not None:
            self.raster.triggerRepaint()
        
    def check_undo_redo_btns(self):
        """Enable/Disable undo and redo buttons based on availability of undo/redo for current raster."""
        self.undo_btn.setDisabled(True)
        self.redo_btn.setDisabled(True)
        
        raster = self.raster
        changes_map = self.changes
        
        if raster is None:
            return
        raster_id = raster.id()
        changes = changes_map.get(raster_id)
        if changes is None:
            return
        
        if changes.nr_undos() > 0:
            self.undo_btn.setEnabled(True)
        if changes.nr_redos() > 0:
            self.redo_btn.setEnabled(True)

    def enable_toolbar_actions(self, enable=True):
        """Enable / disable all toolbar actions but Help (for vectors and unsupported rasters)"""
        actions = self.actions
        always_on = self.actions_always_on
        for widget in actions + [self.width_unit_cbo, self.line_width_sbox]:
            widget.setEnabled(enable)
            if widget in always_on:
                widget.setEnabled(True)
        spin_boxes = self.spin_boxes
        if spin_boxes is not None:
            spin_boxes.enable(enable)

    @staticmethod
    def check_layer(layer):
        """Check if we can work with the raster"""
        if layer is None:
            return False
        if layer.type() != QgsMapLayerType.RasterLayer:
            return False
        if layer.providerType() != 'gdal':
            return False
        if all([
            layer.isValid(),
            layer.crs() is not None,
            check_gdal_driver_create_option(layer),                 # GDAL driver has CREATE option
            os.path.isfile(layer.dataProvider().dataSourceUri()),   # is it a local file?
        ]):
            return True
        else:
            return False

    def set_bands_cbo(self):
        handler = self.handler
        if handler is None:
            if self.logger:
                self.logger.warning("Raster handler is not available")
            return
        self.bands_cbo.currentIndexChanged.disconnect(self.update_active_bands)
        self.bands_cbo.clear()
        for band in handler.bands_range:
            self.bands_cbo.addItem(f"{band}", [band])
        if handler.bands_nr > 1:
            self.bands_cbo.addItem(self.RGB, [1, 2, 3])
        self.bands_cbo.setCurrentIndex(0)
        self.bands_cbo.currentIndexChanged.connect(self.update_active_bands)

    def update_active_bands(self, idx):
        handler = self.handler
        spin_boxes = self.spin_boxes
        if handler is None or spin_boxes is None:
            if self.logger:
                self.logger.warning("Raster handler or band spin boxes are not available")
            return
        
        bands = self.bands_cbo.currentData()
        if bands is None:
            if self.logger:
                self.logger.warning("No active bands selected")
            return
        handler.active_bands = bands
        spin_boxes.create_spinboxes(bands, handler.data_types, handler.nodata_values)
        self.color_btn.setEnabled(len(bands) > 1)
        self.exp_dlg_btn.setEnabled(len(bands) == 1)

    def set_active_raster(self):
        """Active layer has changed - check if it is a raster layer and prepare it for the plugin"""
        spin_boxes = self.spin_boxes
        project = self.project
        changes_map = self.changes

        if changes_map is None:
            if self.logger:
                self.logger.warning("Raster changes map is not available")
            return
        
        old_spin_boxes_values = spin_boxes.get_values() if spin_boxes is not None else []
        self.crs_transform = None
        layer = self.iface.activeLayer()

        if self.check_layer(layer):
            self.raster = layer
            raster = self.raster
            
            if raster is None:
                self.enable_toolbar_actions(enable=False)
                self.reset_raster()
                self.check_undo_redo_btns()
                return

            if project is None:
                if self.logger:
                    self.logger.warning("Project instance is not available")
                self.enable_toolbar_actions(enable=False)
                self.reset_raster()
                self.check_undo_redo_btns()
                return
            
            self.crs_transform = (None if project.crs() == raster.crs() else QgsCoordinateTransform(project.crs(), raster.crs(), project)) 
            handler = RasterHandler(raster, self.uc, self.debug)
            self.handler = handler            
            supported, unsupported_type = handler.write_supported()
            if supported:
                self.enable_toolbar_actions()
                self.set_bands_cbo()                
                spin_boxes = self.spin_boxes
                if spin_boxes is not None:
                    spin_boxes.create_spinboxes(handler.active_bands, handler.data_types, handler.nodata_values)
                    if handler.bands_nr == len(old_spin_boxes_values):
                        spin_boxes.set_values(old_spin_boxes_values)
                elif self.logger:
                    self.logger.warning("Band spin boxes are not available")
                self.bands_cbo.setEnabled(handler.bands_nr > 1)
                self.color_btn.setEnabled(len(handler.active_bands) > 1)
                self.rbounds = raster.extent().toRectF().getCoords()
                handler.raster_changed.connect(self.add_to_undo)

                raster_id = raster.id()
                if changes_map.get(raster_id) is None:
                    changes_map[raster_id] = RasterChanges(nr_to_keep=self.settings["undo_steps"])            
            else:
                msg = f"The raster has unsupported src_data type: {unsupported_type}"
                msg += "\nServal can't work with it, sorry..."
                self.uc.show_warn(msg)
                self.enable_toolbar_actions(enable=False)
                self.reset_raster()
        else:
            # unsupported raster
            self.enable_toolbar_actions(enable=False)
            self.reset_raster()

        self.check_undo_redo_btns()

    def add_to_undo(self, change):
        """Add the old and new blocks to undo stack."""
        raster = self.raster
        changes_map = self.changes
        if raster is None or changes_map is None:
            if self.logger:
                self.logger.warning("Raster or changes map is not available")
            return
        raster_changes = changes_map.get(raster.id())
        if raster_changes is None:
            if self.logger:
                self.logger.warning("No undo stack found for current raster")
            return
        raster_changes.add_change(change)
        self.check_undo_redo_btns()        
        if self.logger:
            self.logger.debug(self.get_undo_redo_values())

    def get_undo_redo_values(self):
        raster = self.raster
        changes_map = self.changes
        
        if raster is None or changes_map is None:
            return "nr undos: 0, redos: 0"
            
        changes = changes_map.get(raster.id())
        if changes is None:
            return "nr undos: 0, redos: 0"
        return f"nr undos: {changes.nr_undos()}, redos: {changes.nr_redos()}"

    def undo(self):
        raster = self.raster
        handler = self.handler
        changes_map = self.changes

        if raster is None or handler is None or changes_map is None:
            if self.logger:
                self.logger.warning("Raster, handler or changes map is not available")
            return

        raster_changes = changes_map.get(raster.id())
        if raster_changes is None:
            if self.logger:
                self.logger.warning("No undo stack found for current raster")
            return

        undo_data = raster_changes.undo()
        handler.write_block_undo(undo_data)
        raster.triggerRepaint()
        self.check_undo_redo_btns()

    def redo(self):
        raster = self.raster
        handler = self.handler
        changes_map = self.changes

        if raster is None or handler is None or changes_map is None:
            if self.logger:
                self.logger.warning("Raster, handler or changes map is not available")
            return

        raster_changes = changes_map.get(raster.id())
        if raster_changes is None:
            if self.logger:
                self.logger.warning("No redo stack found for current raster")
            return

        redo_data = raster_changes.redo()
        handler.write_block_undo(redo_data)
        raster.triggerRepaint()
        self.check_undo_redo_btns()

    def reset_raster(self):
        self.raster = None
        self.handler = None
        self.rbounds = None
        self.crs_transform = None
        self.color_btn.setDisabled(True)

    def color_picker_connection(self, connect=True):
        if connect:
            self.color_btn.colorChanged.connect(self.set_values_from_picker)
        else:
            self.color_btn.colorChanged.disconnect(self.set_values_from_picker)

    @staticmethod
    def show_website():
        QDesktopServices.openUrl(QUrl("https://github.com/lutraconsulting/serval/blob/master/Serval/docs/user_manual.md"))

    def recreate_spatial_index(self, layer):
        """Check if spatial index exists for the layer and if it is relatively old and eventually recreate it."""
        layer_id = layer.id()
        ctime = self.spatial_index_time.get(layer_id)
        if ctime is None or datetime.now() - ctime > timedelta(seconds=30):
            self.spatial_index[layer_id] = QgsSpatialIndex(layer.getFeatures(), None, QgsSpatialIndex.FlagStoreFeatureGeometries)
            self.spatial_index_time[layer_id] = datetime.now()

    def get_nearest_feature(self, pt_feat, vlayer_id):
        """Given the point feature, return nearest feature from vlayer."""
        project = self.project
        if project is None:
            if self.logger:
                self.logger.warning("Project instance is not available")
            return None

        vlayer = project.mapLayer(vlayer_id)
        if vlayer is None:
            if self.logger:
                self.logger.warning(f"Vector layer not found: {vlayer_id}")
            return None

        self.recreate_spatial_index(vlayer)

        spatial_indexes = self.spatial_index
        spatial_index = spatial_indexes.get(vlayer.id())
        if spatial_index is None:
            if self.logger:
                self.logger.warning(f"Spatial index is not available for layer '{vlayer.id()}'")
            return None

        pt_geom = pt_feat.geometry()
        if pt_geom is None or pt_geom.isEmpty():
            if self.logger:
                self.logger.warning("Point feature has no valid geometry")
            return None        
        
        ptxy = pt_geom.asPoint()
        nearest = spatial_index.nearestNeighbor(ptxy)
        if not nearest:
            if self.logger:
                self.logger.warning("No nearest feature found")
            return None

        near_fid = nearest[0]
        return vlayer.getFeature(near_fid)

    def nearest_feature_attr_value(self, pt_feat, vlayer_id, attr_name):
        """Find nearest feature to pt_feat and return its attr_name attribute value."""
        near_feat = self.get_nearest_feature(pt_feat, vlayer_id)
        if near_feat is None:
            if self.logger:
                self.logger.warning(
                    f"Nearest feature not found for layer '{vlayer_id}' and attribute '{attr_name}'")
            return None
        return near_feat[attr_name]

    def nearest_pt_on_line_interpolate_z(self, pt_feat, vlayer_id):
        """Find nearest line feature to pt_feat and interpolate z value from vertices."""
        near_feat = self.get_nearest_feature(pt_feat, vlayer_id)
        if near_feat is None:
            if self.logger:
                self.logger.warning(f"Nearest line feature not found for layer '{vlayer_id}'")
            return None

        near_geom = near_feat.geometry()
        if near_geom is None or near_geom.isEmpty():
            if self.logger:
                self.logger.warning(f"Nearest feature has no valid geometry for layer '{vlayer_id}'")
            return None

        pt_geom = pt_feat.geometry()
        if pt_geom is None or pt_geom.isEmpty():
            if self.logger:
                self.logger.warning("Point feature has no valid geometry")
            return None
        closest_pt_dist = near_geom.lineLocatePoint(pt_geom)
        closest_pt = near_geom.interpolate(closest_pt_dist)
        closest_pt_geom = closest_pt.get()
        if closest_pt_geom is None:
            if self.logger:
                self.logger.warning("Interpolated closest point geometry is not available")
            return None

        return closest_pt_geom.z()

    def intersecting_features_attr_average(self, pt_feat, vlayer_id, attr_name, only_center):
        """
        Find all features intersecting current feature (cell center, or raster cell polygon) and calculate average
        value of their attr_name attribute.
        """
        project = self.project
        handler = self.handler

        if project is None or handler is None:
            if self.logger:
                self.logger.warning("Project instance or raster handler is not available")
            return None

        vlayer = project.mapLayer(vlayer_id)
        if vlayer is None:
            if self.logger:
                self.logger.warning(f"Vector layer not found: {vlayer_id}")
            return None

        self.recreate_spatial_index(vlayer)

        spatial_indexes = self.spatial_index
        spatial_index = spatial_indexes.get(vlayer.id())
        if spatial_index is None:
            if self.logger:
                self.logger.warning(f"Spatial index is not available for layer '{vlayer.id()}'")
            return None

        pt_geom = pt_feat.geometry()
        if pt_geom is None or pt_geom.isEmpty():
            if self.logger:
                self.logger.warning("Point feature has no valid geometry")
            return None
        ptxy = pt_geom.asPoint()
        pt_x, pt_y = ptxy.x(), ptxy.y()
        dxy = 0.001
        half_pix_x = handler.pixel_size_x / 2.0
        half_pix_y = handler.pixel_size_y / 2.0

        if only_center:
            cell = QgsRectangle(pt_x, pt_y, pt_x + dxy, pt_y + dxy)
        else:
            cell = QgsRectangle(
                pt_x - half_pix_x,
                pt_y - half_pix_y,
                pt_x + half_pix_x,
                pt_y + half_pix_y,
            )

        inter_fids = spatial_index.intersects(cell)
        values = []

        for fid in inter_fids:
            feat = vlayer.getFeature(fid)
            feat_geom = feat.geometry()
            if feat_geom is None or not feat_geom.intersects(cell):
                continue

            val = feat[attr_name]
            if not is_number(val):
                continue

            values.append(val)

        if not values:
            return None

        return sum(values) / float(len(values))

    def interpolate_from_mesh(self, pt_feat, mesh_layer_id, group, dataset, above_existing):
        """Interpolate from mesh."""
        project = self.project
        handler = self.handler

        if project is None or handler is None:
            if self.logger:
                self.logger.warning("Project instance or raster handler is not available")
            return None

        mesh_layer = project.mapLayer(mesh_layer_id)
        if mesh_layer is None:
            if self.logger:
                self.logger.warning(f"Mesh layer not found: {mesh_layer_id}")
            return None

        pt_geom = pt_feat.geometry()
        if pt_geom is None or pt_geom.isEmpty():
            if self.logger:
                self.logger.warning("Point feature has no valid geometry")
            return None

        ptxy = pt_geom.asPoint()
        dataset_val = mesh_layer.datasetValue(QgsMeshDatasetIndex(group, dataset), ptxy)
        val = dataset_val.scalar()

        if math.isnan(val):
            return val

        if above_existing:
            ident_vals = handler.provider.identify(ptxy, QgsRaster.IdentifyFormatValue).results()
            if not ident_vals:
                return val

            nodata_values = handler.nodata_values
            if not nodata_values:
                return val

            org_val = list(ident_vals.values())[0]
            if org_val == nodata_values[0]:
                return val

            return max(org_val, val)

        return val
