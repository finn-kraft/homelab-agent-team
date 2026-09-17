import json
import subprocess
from pathlib import Path
import pytest
from control_center.projects import Project, ProjectCatalog

def repository(tmp_path: Path):
    root=tmp_path/"repo";root.mkdir();subprocess.run(["git","init","-q","-b","agents/work"],cwd=root,check=True)
    subprocess.run(["git","config","user.name","Test"],cwd=root,check=True)
    subprocess.run(["git","config","user.email","test@example.invalid"],cwd=root,check=True)
    (root/"docs").mkdir();(root/"docs/roadmap.md").write_text("# Roadmap\n")
    subprocess.run(["git","add","."],cwd=root,check=True);subprocess.run(["git","commit","-qm","initial"],cwd=root,check=True)
    return root

def test_project_catalog_exposes_only_explicit_authorized_repositories(tmp_path):
    root=repository(tmp_path);catalog=ProjectCatalog([Project("demo","Demo",str(root),"agents/work")])
    project=catalog.list()[0]
    assert project["available"] and project["git_status"]=="clean" and project["roadmap_present"]
    with pytest.raises(KeyError):catalog.get("not-authorized")

def test_project_catalog_loads_json_configuration(monkeypatch,tmp_path):
    root=repository(tmp_path)
    monkeypatch.setenv("CONTROL_CENTER_PROJECTS",json.dumps([{"id":"demo","name":"Demo","repository":str(root),"branch":"agents/work"}]))
    assert ProjectCatalog.from_env().get("demo").repository==str(root)

def test_project_ids_are_safe_and_unique(tmp_path):
    root=repository(tmp_path)
    with pytest.raises(ValueError):ProjectCatalog([Project("../bad","Bad",str(root),"agents/work")])
