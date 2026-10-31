"""Los tests corren en un directorio temporal para no ensuciar logs/ del
proyecto con carpetas de tareas falsas (antes se confundían con las de la
corrida de evaluación)."""
import pytest


@pytest.fixture(autouse=True)
def _cwd_temporal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
