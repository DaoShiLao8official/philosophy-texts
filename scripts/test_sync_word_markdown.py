"""Integration tests run with the same Pandoc version as the workflow."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from sync_word_markdown import sync


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.pandoc = os.environ.get("PANDOC", "pandoc")
        self.git("init", "--quiet")

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root)

    def word(self, relative, text):
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([self.pandoc, "-f", "markdown", "-t", "docx", "-o", str(target)],
                       input=text, encoding="utf-8", check=True)
        self.git("add", "--", relative)
        return target

    def test_create_update_rename_delete_and_idempotence(self):
        source = self.word("translations/某作者/测试 文档.docx", "# 初稿\n\n中文正文。\n")
        target = source.with_suffix(".md")
        first = sync(self.root, self.pandoc)
        self.assertEqual(first["documents"], 1)
        self.assertIn("中文正文。", target.read_text("utf-8"))
        word_bytes = source.read_bytes()
        self.assertEqual(sync(self.root, self.pandoc)["updated"], [])
        self.assertEqual(source.read_bytes(), word_bytes)
        self.word("translations/某作者/测试 文档.docx", "# 改稿\n\n修改后的内容。\n")
        sync(self.root, self.pandoc)
        self.assertIn("修改后的内容。", target.read_text("utf-8"))
        renamed = "translations/另一作者/新名称.docx"
        self.word(renamed, "移动后的正文。\n")
        self.git("rm", "-f", "--", source.relative_to(self.root).as_posix())
        sync(self.root, self.pandoc)
        self.assertFalse(target.exists())
        new_target = (self.root / renamed).with_suffix(".md")
        self.assertTrue(new_target.exists())
        self.git("rm", "-f", "--", renamed)
        sync(self.root, self.pandoc)
        self.assertFalse(new_target.exists())

    def test_preserve_notes_formula_table_and_image(self):
        # A tiny valid GIF makes the test self-contained.
        import base64
        (self.root / "image.gif").write_bytes(base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"))
        text = """# 测试

加粗 **文字**，脚注[^1]，公式 $x^2 + y^2$。

| 列一 | 列二 |
| --- | --- |
| 一 | 二 |

![图](image.gif)

[^1]: 脚注内容。
"""
        source_md = self.root / "source.md"
        source_md.write_text(text, "utf-8")
        source = self.root / "writings/测试.docx"
        source.parent.mkdir()
        subprocess.run([self.pandoc, "source.md", "-o", str(source)], cwd=self.root, check=True)
        self.git("add", "writings/测试.docx")
        sync(self.root, self.pandoc)
        markdown = source.with_suffix(".md").read_text("utf-8")
        self.assertIn("脚注内容。", markdown)
        self.assertIn("x^{2} + y^{2}", markdown)
        self.assertIn("| 列一", markdown)
        self.assertIn("_media/", markdown)
        self.assertEqual(len(list((self.root / "writings/_media").glob("*.gif"))), 1)

    def test_bad_word_does_not_overwrite_existing_markdown(self):
        source = self.word("writings/a.docx", "原文。")
        sync(self.root, self.pandoc)
        original = source.with_suffix(".md").read_bytes()
        broken = self.root / "writings/z.docx"
        broken.write_bytes(b"not a Word document")
        self.git("add", "writings/z.docx")
        self.word("writings/a.docx", "不应写入。")
        with self.assertRaises(subprocess.CalledProcessError):
            sync(self.root, self.pandoc)
        self.assertEqual(source.with_suffix(".md").read_bytes(), original)

    def test_unrelated_or_manually_edited_markdown_survives_deletion(self):
        source = self.word("writings/测试.docx", "原文。")
        sync(self.root, self.pandoc)
        target = source.with_suffix(".md")
        target.write_text("手动修改，请保留。", "utf-8")
        unrelated = self.root / "writings/独立文章.md"
        unrelated.write_text("独立文章。", "utf-8")
        self.git("rm", "-f", "--", "writings/测试.docx")
        sync(self.root, self.pandoc)
        self.assertEqual(target.read_text("utf-8"), "手动修改，请保留。")
        self.assertTrue(unrelated.exists())

    def test_uppercase_extension_and_temporary_word_lock(self):
        self.word("writings/大写.DOCX", "大写扩展名。")
        lock = self.root / "writings/~$临时.docx"
        lock.write_bytes(b"lock")
        self.git("add", "--", "writings/~$临时.docx")
        self.assertEqual(sync(self.root, self.pandoc)["documents"], 1)
        self.assertTrue((self.root / "writings/大写.md").exists())
        renamed = self.root / "writings/大写.docx"
        self.git("mv", "-f", "--", "writings/大写.DOCX", "writings/大写.docx")
        sync(self.root, self.pandoc)
        self.assertTrue(renamed.with_suffix(".md").exists())


if __name__ == "__main__":
    unittest.main()
