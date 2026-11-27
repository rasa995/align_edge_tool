"""Build the QGIS plugin ZIP and plugins.xml (custom plugin repository index).

Run from the plugin folder (where metadata.txt lives):
    python build_release.py

Output:
    ../dist/align_edge_v<version>.zip   (upload this to the GitHub Release v<version>)
    plugins.xml                         (commit and push this file)
"""
import configparser
import datetime
import os
import re
import zipfile
from xml.sax.saxutils import escape

PLUGIN_DIR_NAME = "align_edge"          # folder name QGIS installs into
GH_USER = "rasa995"
GH_REPO = "align_edge_tool"
BRANCH = "main"
EXCLUDE = {".git", ".gitignore", ".gitattributes", "plugins.xml",
           "build_release.py", "__pycache__", "LICENSE.bak"}

here = os.path.dirname(os.path.abspath(__file__))
cp = configparser.RawConfigParser(strict=False)
cp.read(os.path.join(here, "metadata.txt"), encoding="utf-8")
m = dict(cp["general"])
version = m["version"].strip()

# ---- ZIP (top-level folder must be the plugin folder name) ----
dist = os.path.join(here, "..", "dist")
os.makedirs(dist, exist_ok=True)
zip_name = f"{PLUGIN_DIR_NAME}_v{version}.zip"
zip_path = os.path.join(dist, zip_name)
with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
    for root, dirs, files in os.walk(here):
        dirs[:] = [d for d in dirs if d not in EXCLUDE]
        for f in files:
            if f in EXCLUDE or f.endswith(".pyc"):
                continue
            full = os.path.join(root, f)
            rel = os.path.relpath(full, here).replace(os.sep, "/")
            z.write(full, f"{PLUGIN_DIR_NAME}/{rel}")

# ---- plugins.xml ----
today = datetime.date.today().isoformat()
xml_path = os.path.join(here, "plugins.xml")
create = today
if os.path.exists(xml_path):
    old = open(xml_path, encoding="utf-8").read()
    mo = re.search(r"<create_date>(.*?)</create_date>", old)
    if mo:
        create = mo.group(1)

raw = f"https://raw.githubusercontent.com/{GH_USER}/{GH_REPO}/{BRANCH}"
download = f"https://github.com/{GH_USER}/{GH_REPO}/releases/download/v{version}/{zip_name}"


def tag(name, value):
    return f"    <{name}>{escape(str(value))}</{name}>\n"


xml = '<?xml version="1.0" encoding="UTF-8"?>\n<plugins>\n'
xml += f'  <pyqgis_plugin name="{escape(m["name"])}" version="{escape(version)}">\n'
xml += tag("description", m.get("description", ""))
xml += tag("about", m.get("about", ""))
xml += tag("version", version)
xml += tag("qgis_minimum_version", m.get("qgisminimumversion", "3.44"))
xml += tag("qgis_maximum_version", m.get("qgismaximumversion", "4.99"))
xml += tag("homepage", m.get("repository", f"https://github.com/{GH_USER}/{GH_REPO}"))
xml += tag("file_name", zip_name)
xml += tag("icon", f"{raw}/{m.get('icon', 'icon.svg')}")
xml += tag("author_name", m.get("author", ""))
xml += tag("download_url", download)
xml += tag("uploaded_by", GH_USER)
xml += tag("create_date", create)
xml += tag("update_date", today)
xml += tag("experimental", m.get("experimental", "False"))
xml += tag("deprecated", "False")
xml += tag("tracker", m.get("tracker", ""))
xml += tag("repository", m.get("repository", ""))
xml += tag("tags", m.get("tags", ""))
xml += tag("changelog", m.get("changelog", ""))
xml += "  </pyqgis_plugin>\n</plugins>\n"
open(xml_path, "w", encoding="utf-8", newline="\n").write(xml)

print(f"Built {os.path.normpath(zip_path)}")
print(f"Wrote {xml_path}")
print(f"Repository URL for QGIS:\n  {raw}/plugins.xml")
print(f"Upload the ZIP to a GitHub Release tagged v{version}")
