import re
import folium
import pandas as pd
import branca.colormap as cm
from pyproj import Geod
from branca.element import MacroElement
from jinja2 import Template

# =========================
# GEODESIC ENGINE
# =========================
geod = Geod(ellps="WGS84")


def create_map(
    gdf,
    column,
    layer_name=None,
    positive=True,
    m=None,
    cmap=None,
    vmin=0,
    vmax=10,
):
    """
    Add one named layer to a Folium map.

    Parameters
    ----------
    gdf        : GeoDataFrame / DataFrame with x, y, bearing, and `column` fields.
    column     : str   – column to colour-encode.
    layer_name : str   – label shown in the LayerControl checkbox and legend.
                         Defaults to `column` if not supplied.
    positive   : bool  – True  → red→yellow→green (higher is better).
                         False → green→yellow→red (lower is better).
    m          : folium.Map or None – pass the map returned by a previous call
                         to keep accumulating layers.
    cmap       : branca.colormap.LinearColormap or None – custom colormap.
    vmin/vmax  : float – colormap scale limits (default 0 / 10).

    Returns
    -------
    folium.Map
    """

    if layer_name is None:
        layer_name = column

    df = gdf.copy()

    # ------------------------------------------------------------------
    # 1.  Initialise the base map on the very first call
    # ------------------------------------------------------------------
    if m is None:
        m = folium.Map(
            location=[df.y.mean(), df.x.mean()],
            zoom_start=18,
            tiles=None,
        )
        folium.TileLayer(
            tiles="https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}",
            attr="Google",
            name="Google Hybrid",
            overlay=True,
            opacity=0.5,
        ).add_to(m)

        # list of (layer_name, cmap) in addition order
        m._legend_items = []
        # name of the first layer – shown by default
        m._default_layer = layer_name

    # ------------------------------------------------------------------
    # 2.  Build / validate the colormap
    # ------------------------------------------------------------------
    if cmap is None:
        colors = ["red", "yellow", "green"] if positive else ["green", "yellow", "red"]
        cmap = cm.LinearColormap(colors, vmin=vmin, vmax=vmax)
    else:
        cmap.vmin = vmin
        cmap.vmax = vmax

    m._legend_items.append((layer_name, cmap))

    # ------------------------------------------------------------------
    # 3.  Feature group  –  name = layer_name (shown in LayerControl)
    # ------------------------------------------------------------------
    group = folium.FeatureGroup(name=layer_name, show=False)
    group.add_to(m)

    # ------------------------------------------------------------------
    # 4.  Drawing helpers
    # ------------------------------------------------------------------
    _skip = {"geometry", "x", "y", "bearing", "path"}
    all_cols = [c for c in df.columns if c not in _skip]

    def _fmt(v):
        try:
            if pd.isna(v):
                return "—"
        except Exception:
            pass
        if isinstance(v, float):
            return f"{v:.3f}"
        return str(v)

    def draw_arrow(lat, lon, bearing, color):
        lon2, lat2, _ = geod.fwd(lon, lat, bearing, 10)
        adj_h = bearing + 30
        folium.PolyLine(
            [(lat, lon), (lat2, lon2)], color="black", weight=7, opacity=1.0
        ).add_to(group)
        folium.RegularPolygonMarker(
            (lat2, lon2), number_of_sides=3, radius=8, rotation=adj_h,
            color="black", fill_color="black", fill_opacity=1,
        ).add_to(group)
        folium.PolyLine(
            [(lat, lon), (lat2, lon2)], color=color, weight=5, opacity=1.0
        ).add_to(group)
        folium.RegularPolygonMarker(
            (lat2, lon2), number_of_sides=3, radius=6, rotation=adj_h,
            color=color, fill_color=color, fill_opacity=1,
        ).add_to(group)

    def add_marker(row, lat, lon, bearing, val):
        color = cmap(val)

        rows_html = "".join(
            f"<tr>"
            f"<td style='padding:1px 8px 1px 0;color:#555;white-space:nowrap;'>{col}</td>"
            f"<td style='padding:1px 0;font-weight:{'bold' if col == column else 'normal'};'>"
            f"{_fmt(row.get(col))}</td>"
            f"</tr>"
            for col in all_cols
        )

        tt = (
            f"<div style='padding:2px;font-family:Arial,sans-serif;font-size:12px;'>"
            f"<table style='border-collapse:collapse;'>{rows_html}</table></div>"
        )

        _img_path = row.get("path", "")
        html_popup = f"""
        <div style="display:flex;flex-direction:row;align-items:flex-start;
                    width:430px;font-family:Arial,sans-serif;font-size:12px;">
            <div style="flex:1;margin-right:12px;">
                <table style="border-collapse:collapse;width:100%;">
                  <thead><tr>
                    <th style="text-align:left;padding:2px 8px 4px 0;
                               border-bottom:1px solid #ccc;color:#777;font-size:11px;">Field</th>
                    <th style="text-align:left;padding:2px 0 4px 0;
                               border-bottom:1px solid #ccc;color:#777;font-size:11px;">Value</th>
                  </tr></thead>
                  <tbody>{rows_html}</tbody>
                </table>
            </div>
            <div style="flex:0 0 auto;">
                <a href="{_img_path}" target="_blank">
                    <img src="{_img_path}"
                         style="width:150px;border:1px solid #ccc;
                                border-radius:4px;cursor:pointer;">
                </a>
            </div>
        </div>"""

        if pd.notna(bearing):
            draw_arrow(lat, lon, bearing, color)

        folium.CircleMarker(
            (lat, lon), radius=8, color="black",
            fill=True, fill_color=color, fill_opacity=1.0,
            opacity=1.0, weight=1,
            tooltip=folium.Tooltip(tt, sticky=True),
            popup=folium.Popup(html_popup, max_width=500),
        ).add_to(group)

    # ------------------------------------------------------------------
    # 5.  Main loop
    # ------------------------------------------------------------------
    for _, row in df.iterrows():
        val = row.get(column)
        if pd.isna(val):
            continue
        add_marker(row, row.y, row.x, row.bearing, val)

    # ------------------------------------------------------------------
    # 6.  Rebuild legend + LayerControl after every call
    # ------------------------------------------------------------------
    _rebuild_legend(m)
    _refresh_layer_control(m)

    return m


# ==========================================================================
# Private helpers
# ==========================================================================

def _safe_id(name):
    """Turn an arbitrary layer name into a safe CSS/JS identifier."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)


def _rebuild_legend(m):
    """
    One legend panel with one <div> per layer, each keyed by layer name.
    JS listens to overlayadd / overlayremove and shows only the matching div.
    On load, only the first (default) layer's legend is visible.
    """
    items = getattr(m, "_legend_items", [])
    if not items:
        return

    # Build one hidden/visible block per layer
    blocks_html = ""
    for i, (lname, cmap_obj) in enumerate(items):
        lid      = _safe_id(lname)
        lo       = cmap_obj.vmin
        hi       = cmap_obj.vmax
        hex_cols = [_to_hex(c) for c in cmap_obj.colors]
        gradient = ", ".join(hex_cols)
        # all layers off by default
        display  = "none"
        blocks_html += f"""
        <div id='legend_block_{lid}'
             style='display:{display};margin-bottom:4px;'>
          <div style='font-weight:bold;font-size:12px;margin-bottom:3px;'>{lname}</div>
          <div style='display:flex;align-items:center;gap:6px;'>
            <span style='font-size:10px;'>{lo}</span>
            <div style='flex:1;height:14px;
              background:linear-gradient(to right,{gradient});
              border:1px solid #aaa;border-radius:3px;'></div>
            <span style='font-size:10px;'>{hi}</span>
          </div>
        </div>"""

    # Build JS mapping:  layer_name → legend block id
    js_map_entries = ", ".join(
        f'"{lname}": "legend_block_{_safe_id(lname)}"'
        for lname, _ in items
    )

    legend_template = Template("""
{% macro script(this, kwargs) %}
var legend_panel = L.control({position: "bottomright"});
legend_panel.onAdd = function(map) {
    var div = L.DomUtil.create("div", "legend_panel");
    div.style.cssText = [
        "background:rgba(255,255,255,0.92)",
        "padding:10px 14px",
        "border-radius:6px",
        "box-shadow:0 2px 6px rgba(0,0,0,.35)",
        "min-width:190px",
        "font-family:Arial,sans-serif",
        "pointer-events:none"
    ].join(";");
    div.innerHTML = `""" + blocks_html + """`;
    return div;
};
legend_panel.addTo({{ this._parent.get_name() }});

// Map: layer display-name → legend block DOM id
var _legend_map = { """ + js_map_entries + """ };

var _active_layers = new Set();

function _refresh_legend() {
    // hide all blocks first
    Object.values(_legend_map).forEach(function(bid) {
        var el = document.getElementById(bid);
        if (el) el.style.display = "none";
    });
    // show one block per active layer
    _active_layers.forEach(function(name) {
        var bid = _legend_map[name];
        if (bid) {
            var el = document.getElementById(bid);
            if (el) el.style.display = "block";
        }
    });
}

{{ this._parent.get_name() }}.on("overlayadd", function(e) {
    if (_legend_map[e.name] !== undefined) _active_layers.add(e.name);
    _refresh_legend();
});
{{ this._parent.get_name() }}.on("overlayremove", function(e) {
    _active_layers.delete(e.name);
    _refresh_legend();
});
{% endmacro %}
""")

    # Remove previously injected legend
    m._children = {
        k: v for k, v in m._children.items()
        if not getattr(v, "_is_legend_panel", False)
    }

    el = MacroElement()
    el._template = legend_template
    el._is_legend_panel = True
    m.add_child(el)


def _refresh_layer_control(m):
    """Remove the existing LayerControl (if any) and add a fresh one."""
    m._children = {
        k: v for k, v in m._children.items()
        if not isinstance(v, folium.LayerControl)
    }
    folium.LayerControl(collapsed=False).add_to(m)


def _to_hex(color):
    """Convert an RGB/RGBA tuple or CSS colour string to #rrggbb hex."""
    if isinstance(color, (list, tuple)):
        r, g, b = [int(c * 255) if c <= 1 else int(c) for c in color[:3]]
        return f"#{r:02x}{g:02x}{b:02x}"
    color = str(color).strip()
    if color.startswith("#"):
        return color
    match = re.match(r"rgba?\((\d+),\s*(\d+),\s*(\d+)", color)
    if match:
        return "#{:02x}{:02x}{:02x}".format(*map(int, match.groups()))
    try:
        import matplotlib.colors as mcolors
        r, g, b, *_ = mcolors.to_rgba(color)
        return f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"
    except Exception:
        return "#888888"