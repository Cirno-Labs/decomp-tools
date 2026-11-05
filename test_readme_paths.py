"""Tests for readme_paths.py: python3 -m unittest test_readme_paths"""
import os
import subprocess
import tempfile
import unittest

import readme_paths

README = """# game

Run `scripts/check.py`, see [the docs](docs/guide.md) and `src/old/`.
Outputs go to `build/out.txt`; the media holds `cd1/disc.iso`.

## Layout

```
src/      decompiled source; src/dll/ is the DLL's code
docs/     documentation
build/    outputs
```
"""


class ReadmePathsTest(unittest.TestCase):
    def repo(self, files):
        root = tempfile.mkdtemp()
        for path, text in files.items():
            os.makedirs(os.path.dirname(os.path.join(root, path)) or root, exist_ok=True)
            with open(os.path.join(root, path), "w") as f:
                f.write(text)
        subprocess.run(["git", "init", "-q", root], check=True)
        subprocess.run(["git", "-C", root, "add", "-A"], check=True)
        return root

    def test_reports_only_missing_tracked_paths(self):
        root = self.repo({
            "README.md": README,
            ".gitignore": "/build/\n",
            "scripts/check.py": "",
            "docs/guide.md": "",
            "src/dll/main.cpp": "",
        })
        found = readme_paths.missing(root, os.path.join(root, "README.md"))
        self.assertEqual([p for _, p in found], ["src/old/"])

    def test_clean_readme(self):
        root = self.repo({"README.md": "See `src/a.cpp`.\n", "src/a.cpp": ""})
        self.assertEqual(readme_paths.missing(root, os.path.join(root, "README.md")), [])


if __name__ == "__main__":
    unittest.main()
