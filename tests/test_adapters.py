"""Adapter failure modes.

Every one of these is a file a real project will hand you eventually. The
contract is the same each time: name the file, say what is wrong, exit
non-zero. Never guess, never half-read.
"""

from __future__ import annotations

import json
import pathlib

import geopandas as gpd
import pytest
from shapely.geometry import LineString

from gisc import adapters
from gisc.adapters import fema, geojson, gpkg, landxml, postgis
from gisc.errors import AdapterError, MissingCRSError, StubError, UsageError

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures"
ALIGN_XML = FIXTURES / "alignment.xml"


def write(path: pathlib.Path, text: str) -> str:
    path.write_text(text)
    return str(path)


def xml(coordgeom: str, header: str = '<CoordinateSystem epsgCode="2264"/>') -> str:
    return f"""<?xml version="1.0"?>
<LandXML xmlns="http://www.landxml.org/schema/LandXML-1.2">
  {header}
  <Alignments name="A"><Alignment name="X1" staStart="0">
    <CoordGeom>{coordgeom}</CoordGeom>
  </Alignment></Alignments>
</LandXML>"""


# -- dispatch --------------------------------------------------------------


@pytest.mark.parametrize(
    "ref, kind",
    [
        ("a.geojson", "geojson"),
        ("a.json", "geojson"),
        ("a.gpkg", "gpkg"),
        ("a.xml", "landxml"),
        ("a.LandXML", "landxml"),
        ("A.GeoJSON", "geojson"),
        ("postgresql://h/db?layer=a.b", "postgis"),
        ("postgres://h/db?layer=a.b", "postgis"),
        ("nfhl:?where=1=1", "fema"),
        ("https://hazards.fema.gov/arcgis/rest/x", "fema"),
    ],
)
def test_kind_detection(ref, kind):
    assert adapters.detect_kind(ref) == kind


@pytest.mark.parametrize("ref", ["pipes.dwg", "survey.dgn", "notes.txt", "noextension"])
def test_unsupported_formats_are_named(ref):
    with pytest.raises(UsageError, match="no adapter"):
        adapters.detect_kind(ref)


def test_unknown_adapter_kind():
    with pytest.raises(AdapterError, match="unknown adapter kind"):
        adapters.get("shapefile")


def test_missing_file_names_the_path(tmp_path):
    with pytest.raises(AdapterError, match="not found"):
        adapters.describe(str(tmp_path / "ghost.geojson"))


# -- geojson ---------------------------------------------------------------


def test_geojson_without_a_crs_member_is_the_rfc_default(tmp_path):
    ref = write(tmp_path / "a.geojson", json.dumps({
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {},
                      "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}}],
    }))
    info = geojson.describe(ref)
    assert info["native_crs"] == "EPSG:4326"
    # An assumption, and labelled as one.
    assert info["crs_source"] == "rfc7946_default"


def test_geojson_with_a_named_crs_member_is_declared(tmp_path):
    ref = write(tmp_path / "a.geojson", json.dumps({
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "EPSG:2264"}},
        "features": [{"type": "Feature", "properties": {},
                      "geometry": {"type": "LineString",
                                   "coordinates": [[905000, 675000], [905100, 675000]]}}],
    }))
    info = geojson.describe(ref)
    assert info["native_crs"] == "EPSG:2264"
    assert info["crs_source"] == "declared"


def test_geojson_crs_override_wins(tmp_path):
    ref = write(tmp_path / "a.geojson", json.dumps({
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {},
                      "geometry": {"type": "LineString",
                                   "coordinates": [[905000, 675000], [905100, 675000]]}}],
    }))
    gdf, prov = geojson.read(ref, crs_override="EPSG:2264")
    assert gdf.crs.to_string() == "EPSG:2264"
    assert prov["crs_source"] == "user_override"


def test_malformed_json_is_not_valid_geojson(tmp_path):
    ref = write(tmp_path / "a.geojson", "{ this is not json")
    with pytest.raises(AdapterError, match="not readable as JSON"):
        geojson.describe(ref)


def test_json_that_is_not_a_feature_collection(tmp_path):
    ref = write(tmp_path / "a.geojson", json.dumps({"hello": "world"}))
    with pytest.raises(AdapterError):
        geojson.read(ref)


def test_geojson_provenance_records_the_file_state(tmp_path):
    ref = str(FIXTURES / "utilities.geojson")
    _gdf, prov = geojson.read(ref)
    assert prov["features_in"] == 3
    assert prov["geometry_types"] == ["LineString"]
    assert len(prov["sha256"]) == 64
    assert prov["bytes"] > 0 and prov["mtime"] and prov["read_at"]


# -- geopackage ------------------------------------------------------------


def test_gpkg_undefined_srs_is_refused_by_name(tmp_path):
    with pytest.raises(MissingCRSError) as exc:
        gpkg.read(str(FIXTURES / "utilities_nocrs.gpkg"))
    assert "utilities_nocrs.gpkg" in str(exc.value)
    assert "will not guess" in str(exc.value)


def test_gpkg_undefined_srs_accepts_an_explicit_crs(tmp_path):
    gdf, prov = gpkg.read(str(FIXTURES / "utilities_nocrs.gpkg"), crs_override="EPSG:2264")
    assert gdf.crs.to_string() == "EPSG:2264"
    assert prov["crs_source"] == "user_override"


def test_gpkg_that_is_not_a_geopackage(tmp_path):
    ref = write(tmp_path / "fake.gpkg", "definitely not sqlite")
    with pytest.raises(AdapterError, match="cannot list GeoPackage layers"):
        gpkg.describe(ref)


def test_gpkg_layer_selection(tmp_path):
    src = gpd.read_file(FIXTURES / "utilities.gpkg")
    path = tmp_path / "multi.gpkg"
    src.to_file(path, layer="water", driver="GPKG")
    src.iloc[:1].to_file(path, layer="sewer", driver="GPKG")

    with pytest.raises(AdapterError, match="has 2 layers"):
        gpkg.describe(str(path))
    with pytest.raises(AdapterError, match="no layer 'gas'"):
        gpkg.describe(str(path), layer="gas")
    assert gpkg.describe(str(path), layer="sewer")["features_declared"] == 1


# -- landxml ---------------------------------------------------------------


def test_landxml_not_xml_at_all(tmp_path):
    with pytest.raises(AdapterError, match="not valid XML"):
        landxml.describe(write(tmp_path / "a.xml", "<LandXML><unclosed>"))


def test_landxml_with_no_alignment(tmp_path):
    doc = '<?xml version="1.0"?><LandXML><CoordinateSystem epsgCode="2264"/></LandXML>'
    with pytest.raises(AdapterError, match="no <Alignment>"):
        landxml.describe(write(tmp_path / "a.xml", doc))


def test_landxml_alignment_with_no_coordgeom(tmp_path):
    doc = ('<?xml version="1.0"?><LandXML><CoordinateSystem epsgCode="2264"/>'
           '<Alignments><Alignment name="X"/></Alignments></LandXML>')
    with pytest.raises(AdapterError, match="no <CoordGeom>"):
        landxml.read(write(tmp_path / "a.xml", doc))


@pytest.mark.parametrize(
    "geom, needle",
    [
        ("<Spiral spiType='clothoid'/>", "Spiral"),
        ("<IrregularLine><PntList2D>0 0 1 1</PntList2D></IrregularLine>", "IrregularLine"),
        ("<Line><Start>0 0</Start></Line>", "Line/End"),
        ("<Line><Start></Start><End>1 1</End></Line>", "Line/Start"),
        ("<Line><Start>north east</Start><End>1 1</End></Line>", "not numeric"),
        ("<Line><Start>5</Start><End>1 1</End></Line>", "at least 2 ordinates"),
        ("<Curve rot='cw'><Start>0 100</Start><Center>0 100</Center>"
         "<End>100 0</End></Curve>", "zero radius"),
        ("<Curve><Start>0 100</Start><Center>0 0</Center><End>100 0</End></Curve>",
         "rot="),
    ],
)
def test_landxml_ugly_geometry_fails_clearly(tmp_path, geom, needle):
    with pytest.raises(AdapterError, match=needle):
        landxml.read(write(tmp_path / "a.xml", xml(geom)))


def test_landxml_refuses_a_discontinuous_alignment(tmp_path):
    """A gap between elements is a broken export, not something to bridge."""
    geom = ("<Line><Start>0 0</Start><End>0 100</End></Line>"
            "<Line><Start>0 150</Start><End>0 250</End></Line>")
    with pytest.raises(AdapterError, match="discontinuous"):
        landxml.read(write(tmp_path / "a.xml", xml(geom)))


def test_landxml_tolerates_a_hairline_gap(tmp_path):
    """0.005 units is rounding in the export, not a real gap."""
    geom = ("<Line><Start>0 0</Start><End>0 100</End></Line>"
            "<Line><Start>0 100.005</Start><End>0 200</End></Line>")
    gdf, prov = landxml.read(write(tmp_path / "a.xml", xml(geom)))
    # The stray start point is snapped away, leaving one continuous polyline...
    assert len(gdf.geometry.iloc[0].coords) == 3
    # ...and the snap is on the record, because it moved the geometry.
    assert any("closed a 0.0050 CRS-unit gap" in n for n in prov["notes"])


def test_landxml_says_nothing_when_there_is_no_gap(tmp_path):
    _gdf, prov = landxml.read(str(ALIGN_XML))
    assert not any("gap" in n for n in prov["notes"])


def test_landxml_no_coordinate_system_at_all(tmp_path):
    with pytest.raises(MissingCRSError, match="will not guess"):
        landxml.describe(write(tmp_path / "a.xml", xml("<Line><Start>0 0</Start>"
                                                       "<End>1 1</End></Line>", header="")))


def test_landxml_unresolvable_crs_name(tmp_path):
    header = '<CoordinateSystem horizontalCoordinateSystemName="Bob\'s Grid"/>'
    with pytest.raises(MissingCRSError):
        landxml.describe(write(tmp_path / "a.xml",
                               xml("<Line><Start>0 0</Start><End>1 1</End></Line>", header)))


def test_landxml_reads_a_clockwise_curve(tmp_path):
    """cw and ccw sweep opposite ways around the same three points."""
    ccw = xml("<Curve rot='ccw'><Start>0 100</Start><Center>0 0</Center>"
              "<End>100 0</End></Curve>")
    cw = xml("<Curve rot='cw'><Start>0 100</Start><Center>0 0</Center>"
             "<End>100 0</End></Curve>")
    a, _ = landxml.read(write(tmp_path / "ccw.xml", ccw))
    b, _ = landxml.read(write(tmp_path / "cw.xml", cw))
    quarter, three_quarter = a.geometry.iloc[0].length, b.geometry.iloc[0].length
    assert quarter == pytest.approx(157.08, abs=0.05)
    assert three_quarter == pytest.approx(471.24, abs=0.15)


def test_landxml_notes_a_length_that_disagrees_with_the_file(tmp_path):
    doc = pathlib.Path(ALIGN_XML).read_text().replace('length="1227.2002"', 'length="999.0"')
    _gdf, prov = landxml.read(write(tmp_path / "a.xml", doc))
    assert any("declared length" in n for n in prov["notes"])


def test_landxml_notes_a_unit_that_disagrees_with_the_crs(tmp_path):
    doc = pathlib.Path(ALIGN_XML).read_text().replace(
        'linearUnit="USSurveyFoot"', 'linearUnit="meter"')
    _gdf, prov = landxml.read(write(tmp_path / "a.xml", doc))
    assert any("linearUnit" in n for n in prov["notes"])
    assert any("used the CRS unit" in n for n in prov["notes"])


def test_landxml_reads_northing_first(tmp_path):
    """LandXML points are 'northing easting'. Getting this backwards is silent."""
    gdf, _ = landxml.read(write(tmp_path / "a.xml",
                                xml("<Line><Start>675000 905000</Start>"
                                    "<End>675000 905100</End></Line>")))
    (x0, y0), (x1, y1) = list(gdf.geometry.iloc[0].coords)
    assert (x0, y0) == (905000, 675000)  # easting, northing
    assert (x1, y1) == (905100, 675000)


# -- stubs -----------------------------------------------------------------


def test_postgis_uri_parsing():
    p = postgis.parse_uri(
        "postgresql://gis@db.example.org:5433/city?layer=util.water&geom=shape&where=dia>8")
    assert p == {"host": "db.example.org", "port": 5433, "user": "gis",
                 "database": "city", "schema": "util", "table": "water",
                 "geom_column": "shape", "where": "dia>8"}


def test_postgis_uri_defaults():
    p = postgis.parse_uri("postgresql://db/city?layer=water")
    assert p["port"] == 5432 and p["schema"] == "public" and p["geom_column"] == "geom"


@pytest.mark.parametrize(
    "uri, needle",
    [
        ("http://db/city?layer=a.b", "not a PostGIS URI"),
        ("postgresql:///city?layer=a.b", "no host"),
        ("postgresql://db?layer=a.b", "no database"),
        ("postgresql://db/city", "name the table"),
    ],
)
def test_postgis_uri_errors(uri, needle):
    with pytest.raises(UsageError, match=needle):
        postgis.parse_uri(uri)


def test_postgis_where_clause_reaches_the_sql():
    sql = postgis.would_run("postgresql://db/city?layer=util.water&where=diameter>8")
    assert "WHERE diameter>8" in sql["features"]
    assert sql["count"].startswith("SELECT count(*)")


def test_postgis_describe_does_not_connect():
    """Compiling a plan against PostGIS must not need the database up."""
    info = postgis.describe("postgresql://gis@nonexistent.invalid:5432/x?layer=a.b")
    assert info["stub"] is True
    assert info["native_crs"] is None  # unknown without a connection, and says so
    assert info["crs_source"] == "unknown"


def test_fema_stub_request_shape():
    req = fema.would_request("nfhl:?where=DFIRM_ID='37021C'",
                             bbox=(-82.6, 35.5, -82.5, 35.6))
    assert req["params"]["geometryType"] == "esriGeometryEnvelope"
    assert req["params"]["where"] == "DFIRM_ID='37021C'"
    assert fema.describe("nfhl:")["layer"] == "28"
    with pytest.raises(StubError, match="not implemented"):
        fema.read("nfhl:")


def test_fema_rejects_a_non_nfhl_reference():
    with pytest.raises(UsageError, match="not an NFHL reference"):
        fema.parse_ref("ftp://example.org/flood")


# -- the read contract holds for every real adapter ------------------------


@pytest.mark.parametrize("ref", [
    str(FIXTURES / "utilities.geojson"),
    str(FIXTURES / "utilities.gpkg"),
    str(FIXTURES / "alignment.xml"),
    str(FIXTURES / "alignment.geojson"),
])
def test_every_read_returns_geometry_and_full_provenance(ref):
    gdf, prov = adapters.read(ref)
    assert gdf.crs is not None
    assert len(gdf) >= 1
    for key in ("kind", "ref", "native_crs", "crs_source", "sha256", "mtime",
                "read_at", "features_in", "geometry_types"):
        assert prov.get(key) is not None, key
    assert prov["features_in"] == len(gdf)


def test_describe_matches_read(tmp_path):
    """The header probe must not disagree with what the full read finds."""
    for ref in (FIXTURES / "utilities.geojson", FIXTURES / "alignment.xml",
                FIXTURES / "utilities.gpkg"):
        info = adapters.describe(str(ref))
        gdf, _prov = adapters.read(str(ref))
        assert gdf.crs.to_string() == info["native_crs"], ref.name


def test_adapters_never_modify_their_source(tmp_path):
    """Read-only means read-only: the bytes are identical afterwards."""
    import hashlib
    import shutil

    for name in ("utilities.gpkg", "utilities.geojson", "alignment.xml"):
        copy = tmp_path / name
        shutil.copy(FIXTURES / name, copy)
        before = hashlib.sha256(copy.read_bytes()).hexdigest()
        adapters.read(str(copy))
        assert hashlib.sha256(copy.read_bytes()).hexdigest() == before, name


def test_reading_an_empty_layer_is_not_a_crash(tmp_path):
    empty = gpd.GeoDataFrame({"name": []}, geometry=[], crs="EPSG:2264")
    path = tmp_path / "empty.gpkg"
    empty.to_file(path, layer="nothing", driver="GPKG")
    gdf, prov = gpkg.read(str(path))
    assert len(gdf) == 0
    assert prov["features_in"] == 0
    assert prov["geometry_types"] == []


def test_a_single_vertex_line_is_not_an_alignment(tmp_path):
    bad = gpd.GeoDataFrame({"n": [1]}, geometry=[LineString([(0, 0), (0, 0)])],
                           crs="EPSG:2264")
    path = tmp_path / "degenerate.geojson"
    bad.to_file(path, driver="GeoJSON")
    gdf, _ = geojson.read(str(path))
    assert gdf.geometry.iloc[0].length == 0


def test_landxml_empty_coordgeom(tmp_path):
    with pytest.raises(AdapterError, match="fewer than 2 points"):
        landxml.read(write(tmp_path / "a.xml", xml("")))


def test_landxml_named_alignment_that_is_not_there(tmp_path):
    with pytest.raises(AdapterError, match="no <Alignment name='CL-GHOST'>"):
        landxml.describe(str(ALIGN_XML), layer="CL-GHOST")


@pytest.mark.parametrize(
    "declared, unit, mismatch",
    [
        ("USSurveyFoot", "US survey foot", False),
        ("foot", "US survey foot", False),   # both feet; the 2 ppm is not a mismatch
        ("meter", "US survey foot", True),
        ("USSurveyFoot", "metre", True),
        ("metre", "metre", False),
        ("chain", "metre", False),           # not a unit we reason about
        (None, "metre", False),
    ],
)
def test_landxml_unit_mismatch_detection(declared, unit, mismatch):
    assert landxml._unit_mismatch(declared, unit) is mismatch
