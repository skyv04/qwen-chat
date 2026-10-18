import unittest
import xml.etree.ElementTree as ET

from qwen_chat.markdown import inline_to_pango, parse_blocks


def well_formed(markup: str) -> bool:
    try:
        ET.fromstring(f"<root>{markup}</root>")
        return True
    except ET.ParseError:
        return False


class ParseBlocksTest(unittest.TestCase):
    def test_unclosed_fence_while_streaming(self):
        blocks = parse_blocks("Intro\n```Python\nprint(1)\n# not a heading")
        self.assertEqual([b.kind for b in blocks], ["paragraph", "code"])
        self.assertEqual(blocks[1].lang, "python")
        self.assertFalse(blocks[1].closed)
        self.assertEqual(blocks[1].text, "print(1)\n# not a heading")

    def test_table_aligns_and_ragged_rows(self):
        blocks = parse_blocks("| a | b | c |\n|:--|:-:|--:|\n| 1 | 2 |\n| x \\| y | 5 | 6 | 7 |\n\nAfter")
        table = blocks[0]
        self.assertEqual(table.kind, "table")
        self.assertEqual(table.aligns, ["left", "center", "right"])
        self.assertEqual(table.rows, [["a", "b", "c"], ["1", "2", ""], ["x | y", "5", "6"]])
        self.assertEqual(blocks[1].text, "After")

    def test_mixed_blocks(self):
        md = "# Title\nText line\n- one\n  more\n\n- two\n1. first\n---\n> quoted\n> still"
        blocks = parse_blocks(md)
        self.assertEqual([b.kind for b in blocks], ["heading", "paragraph", "list", "rule", "quote"])
        self.assertEqual(blocks[2].items, [(0, "-", "one\nmore"), (0, "-", "two"), (0, "1.", "first")])
        self.assertEqual(blocks[4].text, "quoted\nstill")

    def test_pipe_text_over_rule_is_not_a_table(self):
        self.assertEqual([b.kind for b in parse_blocks("a | b\n---")], ["paragraph", "rule"])


class InlineTest(unittest.TestCase):
    def test_formatting_and_links(self):
        out = inline_to_pango("**bold** and *it* and ~~gone~~ see [BBC](https://bbc.com/a?b=1&c=2)")
        self.assertIn("<b>bold</b>", out)
        self.assertIn("<i>it</i>", out)
        self.assertIn("<s>gone</s>", out)
        self.assertIn('<a href="https://bbc.com/a?b=1&amp;c=2">BBC</a>', out)

    def test_code_spans_are_literal(self):
        out = inline_to_pango("run `a **b** <i>` now")
        self.assertIn("a **b** &lt;i&gt;</span>", out)
        self.assertNotIn("<b>", out)

    def test_snake_case_and_unmatched_markers_stay_literal(self):
        self.assertEqual(inline_to_pango("my_var_name"), "my_var_name")
        self.assertEqual(inline_to_pango("2 * 3 and **open"), "2 * 3 and **open")

    def test_always_balanced(self):
        samples = ["**a *b** c*", "<script>&", "`unclosed", "[x](https://a.b/\"q)", "_a **b_ c**",
                   "https://example.com/path_(x)", "~~**x~~**", "a \\*not\\* italic"]
        for s in samples:
            with self.subTest(s=s):
                self.assertTrue(well_formed(inline_to_pango(s)), inline_to_pango(s))


if __name__ == "__main__":
    unittest.main()
