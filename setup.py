"""Bundle the plugin's guide and agent files into the package so an installed ergon finds them."""
import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

ROOT = Path(__file__).resolve().parent


class BuildPy(build_py):
    def run(self):
        super().run()
        out = Path(self.build_lib) / "ergon" / "_plugin"
        (out / "skills" / "ergon").mkdir(parents=True, exist_ok=True)
        (out / "agents").mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / "skills" / "ergon" / "SKILL.md", out / "skills" / "ergon" / "SKILL.md")
        for f in (ROOT / "agents").glob("*.md"):
            shutil.copy(f, out / "agents" / f.name)


setup(cmdclass={"build_py": BuildPy})
