"""Numbering heuristics and accessible course/reader navigation."""

import json
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

from video_to_website import render
from video_to_website.chapters import chapter_number, chapter_groups, lesson_chapter, lesson_numbering


def lesson(name, slug="lesson", **overrides):
    return {
        "slug": slug, "source_name": name + ".mp4", "title": "A generated description",
        "duration": 65, "steps": [], "summary": "", **overrides,
    }


class NavigationParser(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.rows, self.groups, self.current = [], [], []
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "data-course-lesson" in attrs:
            self.rows.append(attrs)
        if "data-chapter-key" in attrs:
            self.groups.append(attrs)
        if attrs.get("aria-current") == "page":
            self.current.append(attrs)


class ChapterTests(unittest.TestCase):
    def test_common_formats_and_zero_padding(self):
        for title in ["4.01 - Intro", "04-002 Next", "  4_03_Overview  ",
                      "4 – 04: Tools", "4.02", "4.2.1 - Details", "Chapter 4.05 - Shape",
                      "Section 4 - Lesson 06: Finish", "chapter 004 part 007 - Export"]:
            with self.subTest(title=title):
                self.assertEqual(chapter_number(title), 4)
        self.assertEqual(chapter_number("0.01 - Setup"), 0)
        self.assertEqual(chapter_number("10-01 - Finish"), 10)

    def test_dates_embedded_numbers_and_plain_order_are_not_chapters(self):
        for title in ["Blender 4.02", "Lesson 4.01", "2026-09-20 - Recording",
                      "20-09-2026 - Recording", "09.20.2026 - Recording",
                      "1 - Introduction", "01 Intro", "4.02GHz CPU", "4.1234 - Long",
                      "4 - 2D Drawing", "", "Chapter 4"]:
            with self.subTest(title=title):
                self.assertIsNone(chapter_number(title))

    def test_renamed_lessons_fall_back_to_source_and_numbered_renames_win(self):
        item = lesson("4.02 - Original", display_title="My introduction")
        self.assertEqual(lesson_numbering(item), (4, 2))
        item["display_title"] = "7-03 - New chapter"
        self.assertEqual(lesson_numbering(item), (7, 3))
        self.assertIsNone(lesson_numbering(lesson("Intro", title="3.14 - Generated description")))
        self.assertEqual(lesson_numbering({"title": "4.10 - Old export"}), (4, 10))

    def test_grouping_collects_scattered_chapters_without_losing_lessons(self):
        lessons = [lesson(name, str(i)) for i, name in enumerate([
            "Welcome", "4.02 Start", "4-03 Middle", "Resources", "10.01 End", "4.01 Revisit",
        ])]
        groups = chapter_groups(lessons)
        self.assertEqual([group["label"] for group in groups], [
            "Other lessons", "Chapter 4", "Chapter 10",
        ])
        self.assertEqual([item["slug"] for group in groups for item in group["lessons"]], ["0", "3", "1", "2", "5", "4"])
        self.assertEqual(groups[1]["key"], "4:1")
        self.assertEqual(chapter_groups([]), [])

    def test_manual_chapters_and_navigation_through_scattered_lessons(self):
        lessons = [lesson("4.01 First", "first"), lesson("5.01 Next chapter", "next"), lesson("4.02 Second", "second")]
        course = {"slug": "course", "title": "Course", "lessons": lessons}
        page = render.render_lesson_page(lessons[0], course)
        self.assertIn('Next: 4.02 Second', page)
        self.assertNotIn('continued', page)
        self.assertEqual([row['data-course-lesson'] for row in NavigationParser(page).rows], ['first', 'second', 'next'])
        self.assertEqual([row['data-position'] for row in NavigationParser(page).rows], ['1', '3', '2'])
        self.assertEqual(lesson_chapter(dict(lessons[0], chapter_override=0)), 0)
        self.assertIsNone(lesson_chapter(dict(lessons[0], chapter_override=-1)))
        self.assertEqual(lesson_chapter(dict(lessons[0], chapter_override=None)), 4)

    def test_native_groups_are_usable_without_javascript(self):
        lessons = [lesson("4.01 First", "first"), lesson("4-02 Second", "second"), lesson("10.01 Third", "third")]
        course = {"slug": "course", "title": "Course", "lessons": lessons}
        page = render.render_course_page(course)
        parsed = NavigationParser(page)
        self.assertEqual([row["data-course-lesson"] for row in parsed.rows], ["first", "second", "third"])
        self.assertEqual([group["data-chapter-key"] for group in parsed.groups], ["4:1", "10:1"])
        self.assertIn("open", parsed.groups[0])
        self.assertNotIn("open", parsed.groups[1])
        self.assertIn("2 lessons &middot; 2m 10s", page)
        self.assertIn('data-course-controls hidden', page)
        self.assertIn('assets/course.js?v=', page)

    def test_unnumbered_course_is_a_flat_list(self):
        page = render.render_course_page({"slug": "course", "title": "Course", "lessons": [lesson("Welcome")]})
        self.assertFalse(NavigationParser(page).groups)
        self.assertIn('<ol class="lesson-list">', page)

    def test_reader_marks_current_lesson_and_opens_its_chapter(self):
        lessons = [lesson("4.01 First", "first"), lesson("10.01 Second", "second", reading_key="stable:instructions")]
        course = {"id": "stable-course", "slug": "course", "title": "Course", "lessons": lessons}
        page = render.render_lesson_page(lessons[1], course)
        parsed = NavigationParser(page)
        self.assertEqual([row["href"] for row in parsed.current], ["second.html"])
        self.assertNotIn("open", parsed.groups[0])
        self.assertIn("open", parsed.groups[1])
        self.assertIn('data-lesson="stable:instructions"', page)
        self.assertIn('data-course="stable-course"', page)
        self.assertIn('data-context="reader" data-density="compact"', page)
        self.assertIn('Course contents &middot; Chapter 10 &middot; Lesson 2 of 2', page)
        self.assertIn('Previous: 4.01 First', page)
        self.assertIn('<details class="course-contents"><summary>', page)

    def test_search_metadata_is_escaped_and_preserves_original_filename(self):
        item = lesson('4.02 - <Original> "file"', display_title='Renamed <img src=x onerror="boom">')
        page = render.render_course_page({"slug": "course", "title": "Course", "lessons": [item]})
        row = NavigationParser(page).rows[0]
        self.assertEqual(row["data-chapter"], "4")
        self.assertEqual(row["data-lesson-number"], "2")
        for term in [item["display_title"], item["source_name"], item["title"], "Chapter 4"]:
            self.assertIn(term, row["data-search"])
        self.assertNotIn('<img src=x', page)

    def test_navigation_asset_is_in_static_exports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            render.write_site(root, [{"slug": "course", "title": "Course", "lessons": [lesson("4.01 First")]}])
            self.assertEqual((root / "assets" / "course.js").read_text(), render.COURSE_SCRIPT)
            self.assertIn('assets/course.js?v=', (root / "course" / "lesson.html").read_text())
            self.assertIn('assets/navigation.js?v=', (root / "course" / "lesson.html").read_text())
            self.assertEqual((root / "assets" / "navigation.js").read_text(), render.NAVIGATION_SCRIPT)
            self.assertEqual(json.loads((root / "site.json").read_text())[0]["lessons"][0]["numbering"], [4, 1])


if __name__ == "__main__":
    unittest.main()
