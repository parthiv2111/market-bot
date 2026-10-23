"""Offline documentation navigation checks; no trading behavior is implemented."""

from pathlib import Path
import re
import unittest
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]


class DocumentationTests(unittest.TestCase):
    def test_local_markdown_links_resolve_inside_repository(self):
        documents = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
        self.assertGreater(len(documents), 1)
        for document in documents:
            for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", document.read_text(encoding="utf-8")):
                parsed = urlsplit(target)
                if parsed.scheme or parsed.netloc or not parsed.path:
                    continue
                with self.subTest(document=document.name, target=target):
                    resolved = (document.parent / unquote(parsed.path)).resolve()
                    self.assertTrue(resolved.is_relative_to(ROOT))
                    self.assertTrue(resolved.is_file(), f"Missing link: {target}")


if __name__ == "__main__":
    unittest.main()
