"""Unit tests for the parts that do not need ffmpeg, whisper, or a live model."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from video_to_website import ingest, progress as progress_mod, render, steps as steps_mod
from video_to_website.llm import LLMError, extract_json
from video_to_website.util import (
    file_fingerprint,
    hms,
    natural_key,
    read_stage,
    slugify,
    title_from_filename,
    write_stage,
)


class ExtractJsonTests(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})

    def test_fenced(self):
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_leading_and_trailing_prose(self):
        text = 'Sure, here is the guide:\n{"a": {"b": [1, 2]}}\nLet me know if you need more.'
        self.assertEqual(extract_json(text), {"a": {"b": [1, 2]}})

    def test_braces_inside_strings(self):
        text = '{"note": "press } then {", "n": 2}'
        self.assertEqual(extract_json(text)["n"], 2)

    def test_truncated_raises(self):
        with self.assertRaises(LLMError):
            extract_json('{"a": [1, 2')

    def test_no_json_raises(self):
        with self.assertRaises(LLMError):
            extract_json("I cannot help with that.")


class MockBackend:
    """Returns one step per window so window planning and merging can be checked."""

    name = "mock"
    model = "mock-1"

    def __init__(self):
        self.calls: list[str] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append(user)
        times = [
            float(line.split("]")[0][1:])
            for line in user.splitlines()
            if line.startswith("[")
        ]
        start, end = min(times), max(times)
        return json.dumps(
            {
                "title": "Mock lesson",
                "summary": "A summary.",
                "prerequisites": [],
                "steps": [
                    {
                        "title": f"Step at {start:.0f}",
                        "start": start,
                        "end": (start + end) / 2,
                        "actions": ["Do the thing"],
                        "shortcuts": ["Ctrl+1"],
                        "note": "",
                    },
                    {
                        "title": f"Step at {(start + end) / 2:.0f}",
                        "start": (start + end) / 2,
                        "end": end,
                        "actions": ["Do the other thing"],
                        "shortcuts": [],
                        "note": "Watch out.",
                    },
                ],
            }
        )


def make_segments(duration: float, every: float = 5.0) -> list[dict]:
    return [
        {"start": t, "end": t + every, "text": f"narration at {t:.0f} seconds"}
        for t in [i * every for i in range(int(duration // every))]
    ]


class ExtractLessonTests(unittest.TestCase):
    def test_short_video_is_one_call(self):
        backend = MockBackend()
        lesson = steps_mod.extract_lesson(
            backend,
            title="Lesson",
            duration=600,
            segments=make_segments(600),
            scene_times=[100.0, 300.0],
            chunk_minutes=25,
        )
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(lesson["title"], "Mock lesson")
        self.assertEqual(len(lesson["steps"]), 2)

    def test_long_video_is_chunked_and_merged(self):
        backend = MockBackend()
        duration = 3600.0
        lesson = steps_mod.extract_lesson(
            backend,
            title="Lesson",
            duration=duration,
            segments=make_segments(duration),
            scene_times=[],
            chunk_minutes=20,
            overlap_seconds=45,
        )
        self.assertGreater(len(backend.calls), 1)
        normalized = steps_mod.normalize_lesson(lesson, duration=duration, fallback_title="Lesson")
        starts = [step["start"] for step in normalized["steps"]]
        self.assertEqual(starts, sorted(starts), "steps must be in time order")
        self.assertTrue(all(step["end"] <= duration for step in normalized["steps"]))
        # Later windows carry absolute timestamps, not window-relative ones.
        self.assertGreater(max(starts), 1800)

    def test_windows_state_their_range_in_the_prompt(self):
        backend = MockBackend()
        steps_mod.extract_lesson(
            backend,
            title="Lesson",
            duration=3600,
            segments=make_segments(3600),
            scene_times=[],
            chunk_minutes=20,
        )
        self.assertIn("of a longer lesson", backend.calls[1])


class NormalizeTests(unittest.TestCase):
    def test_overlaps_are_trimmed(self):
        lesson = {
            "title": "",
            "summary": "",
            "prerequisites": [],
            "steps": [
                {"title": "A", "start": 0, "end": 100, "actions": ["a"]},
                {"title": "B", "start": 50, "end": 150, "actions": ["b"]},
            ],
        }
        out = steps_mod.normalize_lesson(lesson, duration=200, fallback_title="F")
        self.assertEqual(out["steps"][0]["end"], out["steps"][1]["start"])
        self.assertEqual(out["title"], "F")

    def test_garbage_steps_are_dropped(self):
        lesson = {
            "steps": [
                {"title": "ok", "start": 1, "end": 2, "actions": ["x"]},
                {"title": "no times"},
                "not a dict",
                {"title": "", "start": 5, "end": 6, "actions": []},
            ]
        }
        out = steps_mod.normalize_lesson(lesson, duration=100, fallback_title="F")
        self.assertEqual([s["title"] for s in out["steps"]], ["ok"])
        self.assertEqual(out["steps"][0]["index"], 1)

    def test_times_are_clamped_to_duration(self):
        lesson = {"steps": [{"title": "late", "start": 900, "end": 9000, "actions": ["x"]}]}
        out = steps_mod.normalize_lesson(lesson, duration=600, fallback_title="F")
        self.assertLessEqual(out["steps"][0]["end"], 600)
        self.assertLess(out["steps"][0]["start"], 600)


class FrameChoiceTests(unittest.TestCase):
    def test_long_step_gets_several_frames(self):
        step = {"start": 100.0, "end": 200.0}
        entries = [(t, 0.2) for t in (105.0, 120.0, 140.0, 165.0, 190.0)]
        times = steps_mod.frames_for_step(step, entries, duration=600, cap=4)
        self.assertGreaterEqual(len(times), 3)
        self.assertEqual(times, sorted(times))
        self.assertIn(190.0, times, "the step's finished state must be captured")

    def test_short_step_gets_one_frame(self):
        step = {"start": 100.0, "end": 115.0}
        times = steps_mod.frames_for_step(step, [(110.0, 0.4)], duration=600, cap=4)
        self.assertEqual(len(times), 1)

    def test_frames_are_spaced_apart(self):
        step = {"start": 0.0, "end": 120.0}
        entries = [(t, 0.5) for t in (10.0, 10.5, 11.0, 60.0, 119.0)]
        times = steps_mod.frames_for_step(step, entries, duration=600, cap=4, min_spacing=4.0)
        for earlier, later in zip(times, times[1:]):
            self.assertGreaterEqual(later - earlier, 4.0)

    def test_falls_back_to_spread_when_nothing_changed(self):
        step = {"start": 100.0, "end": 200.0}
        times = steps_mod.frames_for_step(step, [], duration=600, cap=3)
        self.assertGreater(len(times), 1)
        self.assertTrue(all(100.0 <= t <= 200.0 for t in times))

    def test_clustered_candidates_are_topped_up(self):
        # Four changes bunched together should not leave a 40s step with one shot.
        step = {"start": 0.0, "end": 40.0}
        entries = [(t, 0.3) for t in (30.0, 30.5, 31.0, 31.5)]
        times = steps_mod.frames_for_step(step, entries, duration=600, cap=4, seconds_per_frame=15.0)
        self.assertGreaterEqual(len(times), 2)
        self.assertIn(31.5, times, "the finished state is still captured")

    def test_density_follows_seconds_per_frame(self):
        step = {"start": 0.0, "end": 60.0}
        sparse = steps_mod.frames_for_step(step, [], duration=600, cap=8, seconds_per_frame=60.0)
        dense = steps_mod.frames_for_step(step, [], duration=600, cap=8, seconds_per_frame=10.0)
        self.assertLess(len(sparse), len(dense))

    def test_never_exceeds_duration(self):
        step = {"start": 0.0, "end": 100.0}
        times = steps_mod.frames_for_step(step, [(99.0, 0.5)], duration=50, cap=2)
        self.assertTrue(all(t < 50 for t in times))


class PromoTests(unittest.TestCase):
    def segments(self, *texts):
        return [
            {"start": index * 10.0, "end": index * 10.0 + 10.0, "text": text}
            for index, text in enumerate(texts)
        ]

    def test_detects_course_plug(self):
        segments = self.segments(
            "now select the top face and press i to inset",
            "check out my free starter course, the link is in the description",
            "back to the mesh, press e to extrude",
        )
        ranges = steps_mod.detect_promo_ranges(segments)
        self.assertEqual(len(ranges), 1)
        self.assertLessEqual(ranges[0][0], 10.0)

    def test_clean_transcript_has_no_promo(self):
        segments = self.segments(
            "press s then y to scale along the y axis",
            "add a subdivision surface modifier with control one",
        )
        self.assertEqual(steps_mod.detect_promo_ranges(segments), [])

    def test_steps_inside_skip_ranges_are_dropped(self):
        steps = [
            {"start": 0.0, "end": 30.0},
            {"start": 100.0, "end": 130.0},
        ]
        kept, dropped = steps_mod.drop_skipped_steps(steps, [(95.0, 140.0)])
        self.assertEqual(dropped, 1)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["start"], 0.0)

    def test_normalize_applies_skip_ranges(self):
        lesson = {
            "skip": [{"start": 90, "end": 140, "reason": "course promo"}],
            "steps": [
                {"title": "real", "start": 0, "end": 30, "actions": ["do it"]},
                {"title": "advert", "start": 100, "end": 130, "actions": ["buy it"]},
            ],
        }
        out = steps_mod.normalize_lesson(lesson, duration=200, fallback_title="F")
        self.assertEqual([s["title"] for s in out["steps"]], ["real"])
        self.assertEqual(out["skip"][0]["reason"], "course promo")


class MotionTests(unittest.TestCase):
    def test_keyword_fallback_spots_movement(self):
        self.assertTrue(
            steps_mod.looks_like_motion(
                {"title": "Slide the loop cut", "actions": ["Drag it towards the edge"]}
            )
        )
        self.assertFalse(
            steps_mod.looks_like_motion(
                {"title": "Enable Clipping", "actions": ["Tick the Clipping checkbox"]}
            )
        )

    def test_model_flag_wins_over_keywords(self):
        lesson = {
            "steps": [
                {
                    "title": "Scale the plane",
                    "start": 0,
                    "end": 10,
                    "actions": ["Press S and scale"],
                    "motion": False,
                },
                {
                    "title": "Enable Clipping",
                    "start": 10,
                    "end": 20,
                    "actions": ["Tick the box"],
                    "motion": True,
                },
            ]
        }
        out = steps_mod.normalize_lesson(lesson, duration=100, fallback_title="F")
        self.assertFalse(out["steps"][0]["motion"])
        self.assertTrue(out["steps"][1]["motion"])

    def test_missing_flag_falls_back_to_keywords(self):
        lesson = {
            "steps": [
                {"title": "Extrude the cover", "start": 0, "end": 10, "actions": ["Press E"]}
            ]
        }
        out = steps_mod.normalize_lesson(lesson, duration=100, fallback_title="F")
        self.assertTrue(out["steps"][0]["motion"])


class ClipWindowTests(unittest.TestCase):
    def segments(self, *spans):
        return [{"start": a, "end": b, "text": "words"} for a, b in spans]

    def test_clip_covers_the_whole_step(self):
        step = {"start": 100.0, "end": 120.0}
        start, end = steps_mod.clip_window(step, [], duration=600, max_seconds=30)
        self.assertLessEqual(start, 100.0, "starts at or just before the step")
        self.assertGreaterEqual(end, 120.0, "runs to at least the step's end")

    def test_consecutive_steps_produce_clips_that_meet(self):
        # The old fixed length left holes between a clip and the next step.
        first = {"start": 100.0, "end": 130.0}
        second = {"start": 130.0, "end": 160.0}
        _, first_end = steps_mod.clip_window(first, [], duration=600, max_seconds=30)
        second_start, _ = steps_mod.clip_window(second, [], duration=600, max_seconds=30)
        self.assertGreaterEqual(first_end, second_start, "no gap between consecutive clips")

    def test_it_does_not_cut_mid_sentence(self):
        step = {"start": 100.0, "end": 120.0}
        segments = self.segments((118.0, 126.0))  # speech straddles the boundary
        _, end = steps_mod.clip_window(step, segments, duration=600, max_seconds=60)
        self.assertGreaterEqual(end, 126.0, "runs to the end of the spoken segment")

    def test_a_long_step_keeps_its_tail(self):
        step = {"start": 0.0, "end": 200.0}
        start, end = steps_mod.clip_window(step, [], duration=600, max_seconds=30)
        self.assertAlmostEqual(end - start, 30.0, places=1)
        self.assertGreater(start, 150.0, "the result of a step is at its end")

    def test_a_tiny_step_still_gets_a_watchable_clip(self):
        step = {"start": 10.0, "end": 10.4}
        start, end = steps_mod.clip_window(step, [], duration=600, min_seconds=1.5)
        self.assertGreaterEqual(end - start, 1.5)

    def test_it_stays_inside_the_video(self):
        step = {"start": 595.0, "end": 600.0}
        start, end = steps_mod.clip_window(step, [], duration=600)
        self.assertGreaterEqual(start, 0.0)
        self.assertLessEqual(end, 600.0)


class StillTrimTests(unittest.TestCase):
    def activity(self, pattern, step=0.1):
        """pattern: list of (seconds, moving?) turned into sampled scores."""
        samples, clock = [], 0.0
        for seconds, moving in pattern:
            for _ in range(int(seconds / step)):
                samples.append((round(clock, 3), 0.05 if moving else 0.0))
                clock += step
        return samples

    def test_a_long_motionless_stretch_is_cut_back(self):
        activity = self.activity([(2, True), (9, False), (3, True)])
        keeps = steps_mod.trim_still_ranges(activity, duration=14.0, max_still=1.5)
        kept = sum(end - start for start, end in keeps)
        self.assertLess(kept, 14.0)
        self.assertAlmostEqual(kept, 6.5, delta=0.4, msg="2s moving + 1.5s still + 3s moving")

    def test_short_pauses_are_left_alone(self):
        activity = self.activity([(2, True), (1, False), (2, True)])
        self.assertIsNone(
            steps_mod.trim_still_ranges(activity, duration=5.0, max_still=1.5),
            "a pause under the limit is not worth a filter",
        )

    def test_a_clip_that_never_moves_is_not_trimmed_to_nothing(self):
        activity = self.activity([(12, False)])
        keeps = steps_mod.trim_still_ranges(activity, duration=12.0, max_still=1.5)
        if keeps is not None:
            self.assertGreaterEqual(sum(end - start for start, end in keeps), 1.0)

    def test_no_samples_means_no_trimming(self):
        self.assertIsNone(steps_mod.trim_still_ranges([], duration=10.0))

    def test_keeps_stay_inside_the_window_and_in_order(self):
        activity = self.activity([(1, True), (5, False), (1, True), (5, False), (1, True)])
        keeps = steps_mod.trim_still_ranges(activity, duration=13.0, max_still=1.5)
        self.assertIsNotNone(keeps)
        for start, end in keeps:
            self.assertGreaterEqual(start, 0.0)
            self.assertLessEqual(end, 13.0)
            self.assertLess(start, end)
        self.assertEqual(keeps, sorted(keeps))


class WatchTests(unittest.TestCase):
    """The service loop: notice a change, let it settle, build once."""

    def decide(self, state, seen, done):
        from video_to_website.cli import _watch_decision

        return _watch_decision(state, seen, done)

    def test_a_new_file_settles_before_it_is_built(self):
        half = (("a.mp4", 100, 1),)
        whole = (("a.mp4", 900, 2),)
        self.assertEqual(self.decide(half, None, None), "settle")
        self.assertEqual(self.decide(whole, half, None), "settle", "still growing")
        self.assertEqual(self.decide(whole, whole, None), "build", "unchanged for a poll")

    def test_it_does_not_rebuild_what_it_already_built(self):
        state = (("a.mp4", 900, 2),)
        self.assertEqual(self.decide(state, state, state), "idle")

    def test_an_empty_library_is_never_built(self):
        self.assertEqual(self.decide((), (), None), "idle")

    def test_a_later_change_builds_again(self):
        first = (("a.mp4", 900, 2),)
        second = first + (("b.mp4", 500, 3),)
        self.assertEqual(self.decide(second, first, first), "settle")
        self.assertEqual(self.decide(second, second, first), "build")

    def test_library_state_follows_size_and_time(self):
        import tempfile
        from pathlib import Path

        from video_to_website.cli import _library_state

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "course").mkdir()
            video = root / "course" / "one.mp4"
            video.write_bytes(b"x" * 10)
            first = _library_state(root)
            self.assertEqual(len(first), 1)
            video.write_bytes(b"x" * 20)
            self.assertNotEqual(_library_state(root), first, "a growing file is a change")
            (root / "course" / "notes.txt").write_text("ignored")
            self.assertEqual(len(_library_state(root)), 1, "only videos count")


class CoverageTests(unittest.TestCase):
    def test_reports_uncovered_stretches(self):
        steps = [{"start": 0, "end": 60}, {"start": 400, "end": 500}]
        gaps = steps_mod.coverage_gaps(steps, 600)
        self.assertEqual(len(gaps), 2)


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.lesson = {
            "slug": "lesson-one",
            "title": "Lesson One",
            "source_name": "01.mp4",
            "duration": 600.0,
            "video_href": "videos/lesson-one.mp4",
            "poster": "frames/lesson-one/step-001.jpg",
            "summary": "Build a mug.",
            "prerequisites": ["Blender 4.2"],
            "steps": [
                {
                    "index": 1,
                    "title": "Delete the <default> cube",
                    "start": 12.0,
                    "end": 30.0,
                    "actions": ["Press X & confirm"],
                    "shortcuts": ["X"],
                    "note": "",
                    "frame": "frames/lesson-one/step-001.jpg",
                }
            ],
            "transcript": [{"start": 12.0, "end": 14.0, "text": "press x"}],
        }
        self.course = {"slug": "course", "title": "A Course", "lessons": [self.lesson]}

    def test_lesson_page_escapes_and_links(self):
        html = render.render_lesson_page(self.lesson, self.course)
        self.assertIn("&lt;default&gt;", html)
        self.assertIn("Press X &amp; confirm", html)
        self.assertIn('data-t="12.00"', html)
        self.assertIn('src="videos/lesson-one.mp4"', html)
        self.assertIn('data-lesson="course:lesson-one"', html)
        self.assertIn("Full transcript", html)

    def test_page_without_video_has_no_player(self):
        lesson = dict(self.lesson, video_href=None)
        html = render.render_lesson_page(lesson, self.course)
        self.assertNotIn("<video", html)
        self.assertIn("0:12", html)

    def _with_shots(self, count):
        frames = [
            {"time": 14.0 + i, "src": f"frames/lesson-one/step-001-{i + 1}.jpg"}
            for i in range(count)
        ]
        return dict(self.lesson, steps=[dict(self.lesson["steps"][0], frames=frames)])

    def test_gallery_renders_every_shot_with_its_own_timestamp(self):
        html = render.render_lesson_page(self._with_shots(2), self.course)
        self.assertIn('data-t="14.00"', html)
        self.assertIn('data-t="15.00"', html)
        self.assertEqual(html.count("<figure>"), 2)

    def test_instructions_sit_under_the_first_visual(self):
        # Reading the actions should not mean looking back to the top of the
        # step, so the first screenshot comes before them and the rest after.
        html = render.render_lesson_page(self._with_shots(3), self.course)
        heading = html.index("<h3>")
        first_shot = html.index("step-001-1.jpg")
        actions = html.index('<ul class="actions">')
        second_shot = html.index("step-001-2.jpg")
        self.assertLess(heading, first_shot, "the title still leads")
        self.assertLess(first_shot, actions, "one visual before the instructions")
        self.assertLess(actions, second_shot, "the remaining visuals follow them")

    def test_the_clip_is_the_visual_the_instructions_follow(self):
        lesson = dict(
            self.lesson,
            steps=[
                dict(
                    self.lesson["steps"][0],
                    frames=[{"time": 20.0, "src": "frames/lesson-one/step-001-1.jpg"}],
                    clip={"src": "clips/lesson-one/step-001.mp4", "time": 15.5, "seconds": 3.5},
                )
            ],
        )
        html = render.render_lesson_page(lesson, self.course)
        self.assertLess(html.index("step-001.mp4"), html.index('<ul class="actions">'))
        self.assertLess(html.index('<ul class="actions">'), html.index("step-001-1.jpg"))

    def test_trailing_shots_share_a_grid(self):
        self.assertIn('class="shots multi"', render.render_lesson_page(self._with_shots(3), self.course))
        self.assertNotIn(
            "shots multi",
            render.render_lesson_page(self._with_shots(2), self.course),
            "one before and one after is not a grid",
        )

    def test_steps_get_the_full_column_and_the_player_floats(self):
        # The steps are the content; the video is reference material, so it
        # must not take a permanent column of its own.
        html = render.render_lesson_page(self.lesson, self.course)
        self.assertNotIn('class="rail"', html)
        self.assertIn('<div class="player" id="player">', html)
        self.assertLess(html.index("<main>"), html.index('class="player"'))
        self.assertIn("position: fixed", render.STYLE)

    def test_player_offers_a_larger_centred_view(self):
        html = render.render_lesson_page(self.lesson, self.course)
        self.assertIn('<div class="player-backdrop" id="player-backdrop"></div>', html)
        self.assertIn('class="player-size"', html)
        self.assertIn("Larger view (f)", html)
        # Centred rather than docked in the corner.
        focus = render.STYLE[render.STYLE.index(".player.focus {") :]
        self.assertIn("transform: translate(-50%, -50%)", focus)
        self.assertIn("94vw", focus)

    def test_column_leaves_room_below_for_the_player(self):
        self.assertIn("calc(24px + var(--player-h))", render.STYLE)

    def test_scrolling_is_quicker_than_the_browser_default(self):
        # window.scrollTo's smooth behaviour has a fixed, slow duration with no
        # way to shorten it, so the page animates the scroll itself.
        script = render.SCRIPT
        self.assertIn("SCROLL_MS", script)
        duration = int(script.split("var SCROLL_MS = ")[1].split(";")[0])
        self.assertLessEqual(duration, 250, "a page step should not take a quarter second")
        self.assertNotIn("behavior: 'smooth'", script)

    def test_playback_speed_is_remembered_across_lessons(self):
        # The per-lesson key is prefixed with the lesson; speed must not be.
        script = render.SCRIPT
        self.assertIn("V2WReaderState.preferences().rate", script)
        self.assertIn("local('v2w:rate')", render.READING_SCRIPT)
        self.assertNotIn("key + ':rate'", script)
        html = render.render_lesson_page(self.lesson, self.course)
        self.assertIn('<span class="rate">1x</span>', html)

    def test_shortcuts_are_listed_on_the_page(self):
        # Whether clicking scrolls is a runtime question, covered by
        # tests/test_app_js.mjs; what the page owes here is a discoverable list.
        html = render.render_lesson_page(self.lesson, self.course)
        self.assertIn('<div class="shortcuts" id="shortcuts" hidden>', html)
        self.assertIn('id="keyhint"', html)
        for keys, _ in render.SHORTCUTS:
            self.assertIn(keys.replace("&", "&amp;"), html)

    def test_every_listed_shortcut_is_actually_handled(self):
        script = render.SCRIPT + render.NAVIGATION_SCRIPT
        named = {
            "\u2193": "ArrowDown",
            "\u2191": "ArrowUp",
            "\u2190": "ArrowLeft",
            "\u2192": "ArrowRight",
            "Space": " ",
            "Esc": "Escape",
        }
        for keys, _ in render.SHORTCUTS:
            for key in keys.split(" / "):
                key = key.split(" (")[0].strip()
                if key.startswith("Shift+"):
                    key = key[len("Shift+"):]
                key = named.get(key, key)
                # A bare letter occurs all over the script, so look for the
                # branch that handles it rather than the character itself.
                self.assertTrue(f"case '{key}':" in script or f"event.key === '{key}'" in script, f"{keys} is listed but has no handler")

    def test_clip_video_has_no_seek_target_of_its_own(self):
        # Clicking the clip pauses it; the caption button is what seeks.
        lesson = dict(
            self.lesson,
            steps=[
                dict(
                    self.lesson["steps"][0],
                    clip={"src": "clips/lesson-one/step-001.mp4", "time": 15.5, "seconds": 3.5},
                )
            ],
        )
        html = render.render_lesson_page(lesson, self.course)
        video_tag = html[html.index("<video src=\"clips/") : html.index("</video>")]
        self.assertNotIn("data-t", video_tag)
        self.assertIn('<button class="ts-mini" data-t="15.50">0:16</button>', html)

    def test_clip_has_visible_controls_not_an_overlaid_hairline(self):
        lesson = dict(
            self.lesson,
            steps=[
                dict(
                    self.lesson["steps"][0],
                    clip={"src": "clips/lesson-one/step-001.mp4", "time": 15.5, "seconds": 3.5},
                )
            ],
        )
        html = render.render_lesson_page(lesson, self.course)
        self.assertIn('<div class="clip-controls">', html)
        self.assertIn('<button class="clip-play" type="button">pause</button>', html)
        self.assertIn('<div class="clip-track"><i></i></div>', html)
        self.assertIn('class="clip-time">0.0 / 3.5s', html)

    def test_images_carry_dimensions_to_stop_reflow(self):
        lesson = dict(
            self.lesson,
            steps=[
                dict(
                    self.lesson["steps"][0],
                    frames=[
                        {
                            "time": 14.0,
                            "src": "frames/lesson-one/step-001-1.jpg",
                            "width": 1280,
                            "height": 720,
                        }
                    ],
                )
            ],
        )
        html = render.render_lesson_page(lesson, self.course)
        self.assertIn('width="1280" height="720"', html)

    def test_missing_dimensions_are_simply_omitted(self):
        html = render.render_lesson_page(self.lesson, self.course)
        self.assertNotIn("width=\"None\"", html)

    def test_each_screenshot_has_its_own_enlarge_control(self):
        # Clicking the picture seeks; reading the small print in it is a
        # separate action and must not take the click away.
        html = render.render_lesson_page(self._with_shots(2), self.course)
        self.assertEqual(html.count('class="zoom"'), 2)
        self.assertIn('data-zoom="frames/lesson-one/step-001-1.jpg" data-at="14.00"', html)
        self.assertIn('<div class="lightbox" id="lightbox" hidden>', html)
        # The enlarge control must not itself be a seek target.
        button = html[html.index('<button class="zoom"') :]
        self.assertNotIn("data-t", button[: button.index("</button>")])

    def test_the_picture_still_seeks(self):
        html = render.render_lesson_page(self._with_shots(2), self.course)
        self.assertIn('data-t="14.00"', html)

    def test_a_hidden_lightbox_really_is_hidden(self):
        # It is laid out with flex, which would otherwise override [hidden].
        self.assertIn(".lightbox[hidden] { display: none; }", render.STYLE)

    def test_assets_are_versioned_so_a_browser_cannot_serve_a_stale_one(self):
        # style.css and app.js never change name, so without this a browser
        # keeps running whichever copy it cached, and the page looks broken in
        # ways that have nothing to do with the build.
        html = render.render_lesson_page(self.lesson, self.course)
        self.assertRegex(html, r'assets/style\.css\?v=[0-9a-f]{8}')
        self.assertRegex(html, r'assets/app\.js\?v=[0-9a-f]{8}')

    def test_the_version_follows_the_content(self):
        first = render._fingerprint(render.SCRIPT)
        self.assertNotEqual(first, render._fingerprint(render.SCRIPT + " "))
        self.assertEqual(first, render._fingerprint(render.SCRIPT))

    def test_indexes_are_versioned_too(self):
        self.assertRegex(render.render_root_index([self.course]), r'style\.css\?v=[0-9a-f]{8}')

    def test_the_loop_control_matches_the_play_button_when_lit(self):
        style = render.STYLE
        play = style[style.index(".clip-play {") : style.index(".clip-play:hover")]
        lit = style[style.index(".clip-loop.on {") : style.index(".clip-loop:hover")]
        for shared in ("var(--accent)", "var(--accent-soft)"):
            self.assertIn(shared, play)
            self.assertIn(shared, lit)

    def test_clips_carry_a_loop_indicator_that_is_also_the_control(self):
        lesson = dict(
            self.lesson,
            steps=[
                dict(
                    self.lesson["steps"][0],
                    clip={"src": "clips/lesson-one/step-001.mp4", "time": 15.5, "seconds": 3.5},
                )
            ],
        )
        html = render.render_lesson_page(lesson, self.course)
        self.assertIn('<button class="clip-loop on" type="button" title="Loop clips (r)">loop</button>', html)
        self.assertIn(".clip-loop.on {", render.STYLE)

    def test_a_trimmed_clip_says_so(self):
        lesson = dict(
            self.lesson,
            steps=[
                dict(
                    self.lesson["steps"][0],
                    clip={
                        "src": "clips/lesson-one/step-001.mp4",
                        "time": 15.5,
                        "seconds": 11.5,
                        "source_seconds": 18.9,
                    },
                )
            ],
        )
        html = render.render_lesson_page(lesson, self.course)
        self.assertIn("12s of 19s, still frames trimmed", html)

    def test_an_untrimmed_clip_does_not(self):
        lesson = dict(
            self.lesson,
            steps=[
                dict(
                    self.lesson["steps"][0],
                    clip={
                        "src": "clips/lesson-one/step-001.mp4",
                        "time": 15.5,
                        "seconds": 3.5,
                        "source_seconds": 3.5,
                    },
                )
            ],
        )
        html = render.render_lesson_page(lesson, self.course)
        self.assertNotIn("still frames trimmed", html)
        self.assertIn("drag the bar to scrub", html)

    def test_clip_renders_as_a_muted_loop(self):
        lesson = dict(
            self.lesson,
            steps=[
                dict(
                    self.lesson["steps"][0],
                    clip={"src": "clips/lesson-one/step-001.mp4", "time": 15.5, "seconds": 3.5},
                )
            ],
        )
        html = render.render_lesson_page(lesson, self.course)
        self.assertIn('src="clips/lesson-one/step-001.mp4"', html)
        self.assertIn("muted", html)
        self.assertIn("loop", html)
        self.assertIn('data-t="15.50"', html)
        self.assertIn("motion", html)

    def test_skipped_ranges_are_shown(self):
        lesson = dict(self.lesson, skip=[{"start": 60.0, "end": 95.0, "reason": "course promo"}])
        html = render.render_lesson_page(lesson, self.course)
        self.assertIn("Left out:", html)
        self.assertIn("course promo", html)
        self.assertIn("1:00-1:35", html)

    def test_markdown_export(self):
        text = render.render_lesson_markdown(self.lesson)
        self.assertIn("# 01\n\nLesson One", text)
        self.assertIn("## 1. Delete the <default> cube", text)
        self.assertIn("`0:12`", text)

    def test_indexes_render(self):
        self.assertIn("A Course", render.render_course_page(self.course))
        page = render.render_root_index([dict(self.course, id="stable-course")])
        self.assertIn("course/index.html", page)
        self.assertIn('data-course-id="stable-course"', page)
        self.assertIn('data-course-slug="course"', page)
        self.assertIn('class="course-activity" href="queue.html" hidden', page)
        self.assertNotIn('id="build-list"', page)
        self.assertNotIn('id="build-status"', page)

    def test_placeholder_index_explains_an_empty_site(self):
        """Without this the server answers an unbuilt site with a bare 403."""
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp) / "site"
            render.write_placeholder(site, "Nothing built yet. Drop a course in /lib.")
            html = (site / "index.html").read_text()
            self.assertIn("Nothing built yet. Drop a course in /lib.", html)
            self.assertTrue((site / "assets" / "style.css").exists())
            self.assertTrue((site / "assets" / "app.js").exists())
            self.assertTrue((site / "queue.html").exists())
            self.assertTrue((site / "assets" / "queue.js").exists())

    def test_queue_page_is_discoverable_and_emitted_on_rebuild(self):
        self.assertIn('href="queue.html"', render.render_root_index([self.course]))
        self.assertIn('href="queue.html"', render.render_upload_page())
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp)
            render.write_site(site, [self.course])
            queue = (site / "queue.html").read_text()
            self.assertIn('id="job-list"', queue)
            self.assertIn('id="job-filter"', queue)
            self.assertIn('id="job-search"', queue)
            self.assertIn('assets/queue.js?v=', queue)

    def test_note_gives_way_to_the_course_count(self):
        html = render.render_root_index([self.course], note="Nothing built yet.")
        self.assertNotIn("Nothing built yet.", html)
        self.assertIn("1 courses", html)


class RetryDelayTests(unittest.TestCase):
    def delay(self, failures, interval=30.0):
        from video_to_website.cli import _retry_delay

        return _retry_delay(failures, interval)

    def test_it_backs_off_rather_than_hammering(self):
        self.assertEqual(self.delay(1), 60.0)
        self.assertEqual(self.delay(2), 120.0)
        self.assertEqual(self.delay(3), 240.0)

    def test_it_stops_at_an_hour(self):
        self.assertEqual(self.delay(50), 3600.0)

    def test_it_always_waits_at_least_one_gap(self):
        """A retry on the very next poll is the thing being avoided."""
        self.assertGreater(self.delay(0), 30.0)


class PruneTests(unittest.TestCase):
    """Renaming a course must not leave the old one on the site forever."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.site = Path(self.tmp.name) / "site"
        for course, lessons in [("old-course", ["one", "two"]), ("keeper", ["three"])]:
            base = self.site / course
            (base / "md").mkdir(parents=True)
            (base / "videos").mkdir(parents=True)
            (base / "index.html").write_text("course")
            for lesson in lessons:
                (base / f"{lesson}.html").write_text("lesson")
                (base / "md" / f"{lesson}.md").write_text("lesson")
                (base / "videos" / f"{lesson}.mp4").write_text("video")
                for kind in ("frames", "clips"):
                    (base / kind / lesson).mkdir(parents=True)
                    (base / kind / lesson / "a.jpg").write_text("asset")
        (self.site / "assets").mkdir()
        (self.site / "assets" / "app.js").write_text("js")
        (self.site / ".work").mkdir()
        (self.site / ".work" / "cache.json").write_text("{}")
        (self.site / "index.html").write_text("root")

    def test_a_course_the_library_no_longer_has_is_removed(self):
        render.prune_site(self.site, {"keeper": {"three"}})
        self.assertFalse((self.site / "old-course").exists())
        self.assertTrue((self.site / "keeper" / "three.html").exists())

    def test_the_cache_and_assets_are_left_alone(self):
        """Pruning walks the output tree, and .work is not output."""
        render.prune_site(self.site, {})
        self.assertTrue((self.site / ".work" / "cache.json").exists())
        self.assertTrue((self.site / "assets" / "app.js").exists())
        self.assertTrue((self.site / "index.html").exists())

    def test_a_removed_lesson_takes_its_assets_with_it(self):
        render.prune_site(self.site, {"old-course": {"one"}, "keeper": {"three"}})
        base = self.site / "old-course"
        self.assertTrue((base / "one.html").exists())
        self.assertFalse((base / "two.html").exists())
        self.assertFalse((base / "md" / "two.md").exists())
        self.assertFalse((base / "frames" / "two").exists())
        self.assertFalse((base / "clips" / "two").exists())
        self.assertFalse((base / "videos" / "two.mp4").exists())
        self.assertTrue((base / "frames" / "one" / "a.jpg").exists())
        self.assertTrue((base / "index.html").exists())

    def test_a_course_that_failed_this_time_keeps_its_pages(self):
        """`keep` is what the build looked at, not what it managed to render."""
        render.write_site(self.site, [], keep={"old-course": {"one", "two"}, "keeper": {"three"}})
        self.assertTrue((self.site / "old-course" / "one.html").exists())
        self.assertTrue((self.site / "keeper" / "three.html").exists())

    def test_nothing_is_pruned_unless_asked(self):
        render.write_site(self.site, [])
        self.assertTrue((self.site / "old-course").exists())

    def test_this_builds_own_output_is_never_pruned(self):
        """A bug in the bookkeeping must not delete what was just written."""
        course = {
            "slug": "fresh",
            "title": "Fresh",
            "lessons": [
                {
                    "slug": "lesson-a", "title": "A", "duration": 10.0, "steps": [],
                    "summary": "", "prerequisites": [], "poster": None,
                    "video_href": None, "source_name": "a.mp4",
                }
            ],
        }
        render.write_site(self.site, [course], keep={})
        self.assertTrue((self.site / "fresh" / "lesson-a.html").exists())
        self.assertFalse((self.site / "old-course").exists())


class SafeComponentTests(unittest.TestCase):
    """Every upload name arrives from a browser, so this is the boundary."""

    def test_separators_and_traversal_are_refused(self):
        for bad in ["..", ".", "", "   ", "a/b", "a\\b", "../../etc/passwd", "....", " . "]:
            self.assertIsNone(ingest.safe_component(bad), bad)

    def test_a_leading_dot_cannot_survive(self):
        """A dotfile would hide the upload from the watcher that builds it."""
        self.assertEqual(ingest.safe_component(".hidden.mp4"), "hidden.mp4")

    def test_ordinary_names_come_through_intact(self):
        self.assertEqual(ingest.safe_component("01_car_body.mp4"), "01_car_body.mp4")
        self.assertEqual(ingest.safe_component("Lesson 3 - Wheels.mp4"), "Lesson 3 - Wheels.mp4")
        self.assertEqual(ingest.safe_component("_ Café 日本語.mp4"), "_ Café 日本語.mp4")

    def test_invisible_download_markers_do_not_become_underscores(self):
        expected = "4.08 - Car Body - Base Shape.mp4"
        for name in [
            "\ufe0f " + expected,
            "%EF%B8%8F%20" + expected,
            " \ufeff\u200b\ufe0f " + expected,
            "\u200e" + expected + "\u200f",
            expected + "\U000e0100",
            "4.08 - Car\u200b Body - Base Shape.mp4",
        ]:
            with self.subTest(name=name):
                self.assertEqual(ingest.safe_component(name), expected)

    def test_invisible_markers_do_not_bypass_path_validation(self):
        for name in ["\ufe0f", "\ufeff \u200b", "\ufe0f ..", "\u200b..%2fetc", "\ufe0f..\\etc"]:
            with self.subTest(name=name):
                self.assertIsNone(ingest.safe_component(name))

    def test_url_encoding_is_undone_before_checking(self):
        self.assertIsNone(ingest.safe_component("..%2f..%2fetc"))

    def test_awkward_characters_are_replaced_not_dropped(self):
        self.assertEqual(ingest.safe_component("a:b*c?.mp4"), "a_b_c_.mp4")

    def test_names_are_bounded(self):
        self.assertEqual(len(ingest.safe_component("x" * 500)), 120)


class UploadServerTests(unittest.TestCase):
    """Driven over a real socket: the handler's job is mostly HTTP."""

    def setUp(self):
        import http.server
        import threading

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.library = Path(self.tmp.name) / "library"
        self.library.mkdir()
        self.work = Path(self.tmp.name) / "work"
        self.work.mkdir()

        http.server.ThreadingHTTPServer.allow_reuse_address = True
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ingest.IngestHandler)
        self.httpd.library = self.library
        self.httpd.work = self.work
        # A short poll interval only so shutdown() in teardown is not a half
        # second of waiting per test.
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self.thread.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.port = self.httpd.server_address[1]

    def request(self, method, path, body=None, headers=None):
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            res = conn.getresponse()
            return res.status, json.loads(res.read() or b"{}")
        finally:
            conn.close()

    def put(self, path, body=b"video bytes"):
        return self.request("PUT", path, body=body, headers={"Content-Length": str(len(body))})

    def post(self, path, payload):
        body = json.dumps(payload).encode()
        return self.request(
            "POST",
            path,
            body=body,
            headers={"Content-Length": str(len(body)), "Content-Type": "application/json"},
        )

    def lesson(self, course, name, content=b"video"):
        (self.library / course).mkdir(parents=True, exist_ok=True)
        (self.library / course / name).write_bytes(content)

    def cache_for(self, course, stem):
        directory = self.work / course / stem
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "transcript.json").write_text("{}")
        return directory

    def test_an_upload_lands_in_the_library(self):
        status, payload = self.put("/api/library/my_course/lesson_one.mp4", b"abc123")
        self.assertEqual(status, 201)
        self.assertEqual(payload["bytes"], 6)
        landed = self.library / "my_course" / "lesson_one.mp4"
        self.assertEqual(landed.read_bytes(), b"abc123")

    def test_invisible_prefix_is_removed_and_name_collisions_are_refused(self):
        path = "/api/library/course/%EF%B8%8F%204.08%20-%20Car%20Body.mp4"
        status, payload = self.put(path, b"original")
        self.assertEqual(status, 201)
        self.assertEqual(payload["name"], "4.08 - Car Body.mp4")
        landed = self.library / "course" / payload["name"]
        self.assertEqual(landed.read_bytes(), b"original")
        status, _ = self.put("/api/library/course/4.08%20-%20Car%20Body.mp4", b"replacement")
        self.assertEqual(status, 409)
        self.assertEqual(landed.read_bytes(), b"original")

    def test_existing_invisible_names_are_still_looked_up_exactly(self):
        self.lesson("course", "\ufe0f source.mp4", b"imported")
        self.lesson("course", "source.mp4", b"other")
        status, _ = self.request("DELETE", "/api/library/course/%EF%B8%8F%20source.mp4")
        self.assertEqual(status, 200)
        self.assertEqual((self.library / "course" / "source.mp4").read_bytes(), b"other")

    def test_nothing_partial_is_left_behind(self):
        self.put("/api/library/my_course/lesson_one.mp4")
        leftovers = list((self.library / "my_course").glob(".incoming*"))
        self.assertEqual(leftovers, [])

    def test_traversal_cannot_escape_the_library(self):
        status, _ = self.put("/api/library/..%2f..%2fetc/passwd.mp4")
        self.assertEqual(status, 400)
        self.assertEqual(list(self.library.iterdir()), [])

    def test_only_video_files_are_accepted(self):
        status, payload = self.put("/api/library/my_course/notes.txt")
        self.assertEqual(status, 415)
        self.assertIn(".txt", payload["error"])

    def test_an_existing_lesson_is_not_silently_replaced(self):
        self.put("/api/library/my_course/lesson_one.mp4", b"first")
        status, _ = self.put("/api/library/my_course/lesson_one.mp4", b"second")
        self.assertEqual(status, 409)
        self.assertEqual((self.library / "my_course" / "lesson_one.mp4").read_bytes(), b"first")

    def test_replacing_on_purpose_works(self):
        self.put("/api/library/my_course/lesson_one.mp4", b"first")
        status, _ = self.put("/api/library/my_course/lesson_one.mp4?overwrite=1", b"second")
        self.assertEqual(status, 201)
        self.assertEqual((self.library / "my_course" / "lesson_one.mp4").read_bytes(), b"second")

    def test_an_empty_body_is_refused(self):
        status, _ = self.put("/api/library/my_course/lesson_one.mp4", b"")
        self.assertEqual(status, 413)

    def test_existing_names_are_not_rewritten_when_deleting(self):
        self.lesson("course", "What's next.mp4", b"original")
        self.lesson("course", "What_s next.mp4", b"different")
        status, _ = self.request("DELETE", "/api/library/course/What%27s%20next.mp4")
        self.assertEqual(status, 200)
        self.assertFalse((self.library / "course" / "What's next.mp4").exists())
        self.assertEqual((self.library / "course" / "What_s next.mp4").read_bytes(), b"different")

    def test_existing_course_name_survives_rename_and_upload(self):
        self.lesson("O'Reilly", "one.mp4")
        status, _ = self.post("/api/library/O%27Reilly/one.mp4", {"to_name": "two.mp4"})
        self.assertEqual(status, 200)
        self.assertTrue((self.library / "O'Reilly" / "two.mp4").exists())
        status, _ = self.put("/api/library/O%27Reilly/three.mp4")
        self.assertEqual(status, 201)
        self.assertTrue((self.library / "O'Reilly" / "three.mp4").exists())

    def test_symlink_course_cannot_escape_the_library(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "source.mp4").write_bytes(b"keep")
        (self.library / "escape").symlink_to(outside, target_is_directory=True)
        status, _ = self.request("DELETE", "/api/library/escape/source.mp4")
        self.assertEqual(status, 400)
        self.assertTrue((outside / "source.mp4").exists())

    def test_concurrent_uploads_do_not_share_temporary_files(self):
        import http.client
        import time

        connections = [http.client.HTTPConnection("127.0.0.1", self.port, timeout=5) for _ in range(2)]
        for conn in connections:
            self.addCleanup(conn.close)
            conn.putrequest("PUT", "/api/library/course/race.mp4")
            conn.putheader("Content-Length", "8")
            conn.endheaders()
            conn.send(b"1234")
        deadline = time.monotonic() + 3
        while len(list((self.library / "course").glob(".incoming-*"))) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(list((self.library / "course").glob(".incoming-*"))), 2)
        connections[0].send(b"AAAA")
        first = connections[0].getresponse()
        first.read()
        connections[1].send(b"BBBB")
        second = connections[1].getresponse()
        second.read()
        self.assertEqual((first.status, second.status), (201, 409))
        self.assertEqual((self.library / "course" / "race.mp4").read_bytes(), b"1234AAAA")

    def test_job_actions_use_the_durable_catalog(self):
        from video_to_website.catalog import Catalog
        self.lesson("course", "one.mp4")
        self.httpd.catalog = Catalog(Path(self.tmp.name) / "state")
        self.httpd.catalog.reconcile(self.library, {})
        lesson = self.httpd.catalog.rows("SELECT * FROM lessons")[0]
        status, payload = self.request("GET", "/api/jobs")
        self.assertEqual(status, 200)
        self.assertEqual(payload["videos"][0]["id"], lesson["id"])
        self.post(f"/api/lessons/{lesson['id']}/cancel", {})
        self.assertFalse(self.httpd.catalog.is_current(lesson["desired_build"]))
        status, retried = self.post(f"/api/lessons/{lesson['id']}/retry", {})
        self.assertEqual(status, 200)
        self.assertNotEqual(retried["build_id"], lesson["desired_build"])

    def test_the_course_list_is_offered_for_the_dropdown(self):
        (self.library / "course_a").mkdir()
        (self.library / "course_b").mkdir()
        (self.library / ".hidden").mkdir()
        status, payload = self.request("GET", "/api/courses")
        self.assertEqual(status, 200)
        self.assertEqual(payload["courses"], ["course_a", "course_b"])

    def test_a_folder_it_cannot_write_says_what_to_do(self):
        """The case a root scp leaves behind: readable, not writable."""
        import os

        if os.geteuid() == 0:
            self.skipTest("root ignores the permissions this is about")
        locked = self.library / "from_scp"
        locked.mkdir()
        locked.chmod(0o555)
        self.addCleanup(locked.chmod, 0o755)
        status, payload = self.put("/api/library/from_scp/lesson.mp4")
        self.assertEqual(status, 403)
        self.assertIn("cannot write into from_scp/", payload["error"])
        self.assertIn("chown -R", payload["error"])
        self.assertIn(str(self.library), payload["error"])

    def test_the_library_listing_is_in_build_order(self):
        """Which is the point of showing it: ordering is what goes wrong."""
        for name in ["lesson 10.mp4", "lesson 2.mp4", "notes.txt", ".hidden.mp4"]:
            self.lesson("a_course", name)
        status, payload = self.request("GET", "/api/library")
        self.assertEqual(status, 200)
        self.assertEqual(payload["courses"][0]["name"], "a_course")
        names = [v["name"] for v in payload["courses"][0]["videos"]]
        self.assertEqual(names, ["lesson 2.mp4", "lesson 10.mp4"])
        self.assertEqual(payload["courses"][0]["videos"][0]["bytes"], 5)

    def test_a_video_can_be_deleted(self):
        self.lesson("a_course", "one.mp4")
        self.lesson("a_course", "two.mp4")
        status, _ = self.request("DELETE", "/api/library/a_course/one.mp4")
        self.assertEqual(status, 200)
        self.assertFalse((self.library / "a_course" / "one.mp4").exists())
        self.assertTrue((self.library / "a_course" / "two.mp4").exists())

    def test_the_last_deletion_takes_the_course_with_it(self):
        self.lesson("a_course", "only.mp4")
        self.request("DELETE", "/api/library/a_course/only.mp4")
        self.assertFalse((self.library / "a_course").exists())

    def test_deleting_keeps_the_cache_for_if_it_comes_back(self):
        self.lesson("a_course", "one.mp4")
        cache = self.cache_for("a-course", "one")
        self.request("DELETE", "/api/library/a_course/one.mp4")
        self.assertTrue((cache / "transcript.json").exists())

    def test_deleting_something_that_is_not_there(self):
        self.assertEqual(self.request("DELETE", "/api/library/a_course/gone.mp4")[0], 404)

    def test_renaming_moves_the_file(self):
        self.lesson("a_course", "4.02 - board.mp4")
        status, payload = self.post("/api/library/a_course/4.02%20-%20board.mp4", {"to_name": "02 - board.mp4"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["name"], "02 - board.mp4")
        self.assertTrue((self.library / "a_course" / "02 - board.mp4").exists())
        self.assertFalse((self.library / "a_course" / "4.02 - board.mp4").exists())

    def test_renaming_carries_the_cache_so_reordering_is_free(self):
        """Otherwise fixing the numbering re-transcribes the whole course."""
        self.lesson("a_course", "4.02 - board.mp4")
        # Slugs as the builder makes them: slugify drops the dot.
        self.cache_for("a-course", "402-board")
        self.post("/api/library/a_course/4.02%20-%20board.mp4", {"to_name": "02 - board.mp4"})
        self.assertTrue((self.work / "a-course" / "02-board" / "transcript.json").exists())
        self.assertFalse((self.work / "a-course" / "402-board").exists())

    def test_a_video_can_move_to_another_course(self):
        self.lesson("a_course", "one.mp4")
        status, _ = self.post("/api/library/a_course/one.mp4", {"to_course": "b_course"})
        self.assertEqual(status, 200)
        self.assertTrue((self.library / "b_course" / "one.mp4").exists())
        self.assertFalse((self.library / "a_course").exists())

    def test_renaming_onto_an_existing_lesson_is_refused(self):
        self.lesson("a_course", "one.mp4", b"first")
        self.lesson("a_course", "two.mp4", b"second")
        status, _ = self.post("/api/library/a_course/one.mp4", {"to_name": "two.mp4"})
        self.assertEqual(status, 409)
        self.assertEqual((self.library / "a_course" / "two.mp4").read_bytes(), b"second")
        self.assertTrue((self.library / "a_course" / "one.mp4").exists())

    def test_renaming_cannot_escape_the_library(self):
        self.lesson("a_course", "one.mp4")
        status, _ = self.post("/api/library/a_course/one.mp4", {"to_course": "../../etc"})
        self.assertEqual(status, 400)
        self.assertTrue((self.library / "a_course" / "one.mp4").exists())

    def test_renaming_to_something_that_is_not_a_video(self):
        self.lesson("a_course", "one.mp4")
        self.assertEqual(self.post("/api/library/a_course/one.mp4", {"to_name": "one.txt"})[0], 415)

    def test_unknown_endpoints_say_so(self):
        self.assertEqual(self.request("GET", "/api/nope")[0], 404)
        self.assertEqual(self.put("/api/elsewhere/a/b.mp4")[0], 404)

    def test_uploads_are_refused_when_no_library_is_mounted(self):
        self.httpd.library = None
        self.assertEqual(self.put("/api/library/c/a.mp4")[0], 503)
        self.assertEqual(self.request("GET", "/api/courses")[0], 503)


class StageCacheTests(unittest.TestCase):
    """Moving a course must not throw away the transcription work."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.video = self.root / "a.mp4"
        self.video.write_bytes(b"pretend this is video")
        self.cache = self.root / "probe.json"
        self.params = {"stage": "probe"}

    def test_a_moved_file_keeps_its_fingerprint(self):
        before = file_fingerprint(self.video)
        moved = self.root / "elsewhere" / "a.mp4"
        moved.parent.mkdir()
        self.video.rename(moved)
        self.assertEqual(file_fingerprint(moved), before)

    def test_the_cache_survives_the_move(self):
        write_stage(self.cache, file_fingerprint(self.video), self.params, {"duration": 12})
        moved = self.root / "elsewhere" / "a.mp4"
        moved.parent.mkdir()
        self.video.rename(moved)
        self.assertEqual(
            read_stage(self.cache, file_fingerprint(moved), self.params), {"duration": 12}
        )

    def test_a_cache_from_an_older_build_still_counts(self):
        """Those carry a path field this build no longer asks about."""
        legacy = dict(file_fingerprint(self.video), path=str(self.video))
        write_stage(self.cache, legacy, self.params, {"duration": 12})
        self.assertEqual(
            read_stage(self.cache, file_fingerprint(self.video), self.params), {"duration": 12}
        )

    def test_edited_content_still_misses(self):
        write_stage(self.cache, file_fingerprint(self.video), self.params, {"duration": 12})
        self.video.write_bytes(b"a different video entirely")
        self.assertIsNone(read_stage(self.cache, file_fingerprint(self.video), self.params))

    def test_different_stage_params_still_miss(self):
        write_stage(self.cache, file_fingerprint(self.video), self.params, {"duration": 12})
        other = read_stage(self.cache, file_fingerprint(self.video), {"stage": "probe", "v": 2})
        self.assertIsNone(other)


class ProgressTests(unittest.TestCase):
    """The status file is the only thing the page can see a build through."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.site = Path(self.tmp.name) / "site"
        self.courses = [
            {"title": "A Course", "videos": [Path("/lib/car_body.mp4"), Path("/lib/b.mp4")]}
        ]

    def status(self) -> dict:
        return json.loads((self.site / progress_mod.STATUS_NAME).read_text())

    def test_plan_queues_every_video(self):
        p = progress_mod.Progress(self.site)
        p.plan(self.courses)
        data = self.status()
        self.assertTrue(data["building"])
        self.assertEqual([v["state"] for v in data["videos"]], ["queued", "queued"])
        self.assertEqual([v["course"] for v in data["videos"]], ["A Course", "A Course"])
        self.assertEqual(data["videos"][0]["title"], "car body")

    def test_stages_advance_the_current_video(self):
        p = progress_mod.Progress(self.site)
        p.plan(self.courses)
        p.start(Path("/lib/car_body.mp4"))
        p.stage("transcribe")
        first = self.status()["videos"][0]
        self.assertEqual(first["state"], "working")
        self.assertEqual(first["label"], "Transcribing the audio")
        self.assertEqual(first["step"], 2)
        self.assertEqual(first["steps"], len(progress_mod.STAGE_LABELS))
        # The one that has not started yet must not have moved.
        self.assertEqual(self.status()["videos"][1]["state"], "queued")

    def test_finish_records_the_outcome(self):
        p = progress_mod.Progress(self.site)
        p.plan(self.courses)
        p.start(Path("/lib/car_body.mp4"))
        p.finish("failed")
        self.assertEqual(self.status()["videos"][0]["state"], "failed")
        # A stage call after the video is done belongs to nobody, and is dropped
        # rather than landing on the wrong row.
        p.stage("frames")
        self.assertEqual(self.status()["videos"][0]["stage"], None)

    def test_finish_build_moves_the_stamp_the_page_watches(self):
        p = progress_mod.Progress(self.site)
        p.plan(self.courses)
        before = self.status()["built"]
        p.finish_build()
        after = self.status()
        self.assertGreater(after["built"], before)
        self.assertFalse(after["building"])
        self.assertEqual([v["state"] for v in after["videos"]], ["done", "done"])

    def test_a_failed_build_does_not_move_the_stamp(self):
        """Otherwise every open page reloads to show the same pages as before."""
        p = progress_mod.Progress(self.site)
        p.plan(self.courses)
        p.finish_build()
        good = self.status()["built"]
        p.plan(self.courses)
        p.start(Path("/lib/car_body.mp4"))
        p.fail_build("the API returned 529", retry_in=60)
        after = self.status()
        self.assertEqual(after["built"], good)
        self.assertFalse(after["building"])
        self.assertEqual(after["error"], "the API returned 529")
        self.assertEqual(after["retry_in"], 60)
        self.assertEqual(after["videos"][0]["state"], "failed")
        # The one that never started is still owed a run.
        self.assertEqual(after["videos"][1]["state"], "queued")

    def test_a_new_build_clears_the_last_failure(self):
        p = progress_mod.Progress(self.site)
        p.plan(self.courses)
        p.fail_build("boom", retry_in=30)
        p.plan(self.courses)
        self.assertIsNone(self.status()["error"])
        self.assertIsNone(self.status()["retry_in"])

    def test_the_stamp_survives_a_restart(self):
        """Otherwise every service restart reloads every open browser."""
        first = progress_mod.Progress(self.site)
        first.plan(self.courses)
        first.finish_build()
        stamp = self.status()["built"]
        self.assertEqual(progress_mod.Progress(self.site).built, stamp)

    def test_queue_labels_files_that_are_still_arriving(self):
        p = progress_mod.Progress(self.site)
        p.queue([Path("/lib/course/a.mp4")], "Waiting for the copy to finish")
        entry = self.status()["videos"][0]
        self.assertEqual(entry["state"], "queued")
        self.assertEqual(entry["label"], "Waiting for the copy to finish")
        self.assertEqual(entry["course"], "course")
        self.assertFalse(self.status()["building"])

    def test_no_site_directory_means_no_writing(self):
        p = progress_mod.Progress(None)
        p.plan(self.courses)
        p.start(Path("/lib/car_body.mp4"))
        p.stage("scenes")
        p.finish_build()
        self.assertFalse(self.site.exists())

    def test_nothing_is_left_half_written(self):
        p = progress_mod.Progress(self.site)
        p.plan(self.courses)
        p.finish_build()
        self.assertEqual(list(self.site.glob("*.tmp")), [])


class RangeRequestTests(unittest.TestCase):
    """Video seeking depends on these, so they are worth pinning down."""

    def parse(self, header: str, size: int = 1000):
        from video_to_website.cli import _RangeHandler

        handler = _RangeHandler.__new__(_RangeHandler)
        handler.headers = {"Range": header}
        return _RangeHandler._parse_range(handler, size)

    def test_explicit_range(self):
        self.assertEqual(self.parse("bytes=0-499"), (0, 499))

    def test_open_ended_range(self):
        self.assertEqual(self.parse("bytes=900-"), (900, 999))

    def test_suffix_range(self):
        self.assertEqual(self.parse("bytes=-100"), (900, 999))

    def test_end_is_clamped_to_file_size(self):
        self.assertEqual(self.parse("bytes=500-99999"), (500, 999))

    def test_unsatisfiable_and_malformed(self):
        self.assertIsNone(self.parse("bytes=5000-"))
        self.assertIsNone(self.parse("bytes=800-700"))
        self.assertIsNone(self.parse("items=0-10"))
        self.assertIsNone(self.parse("bytes=abc-def"))


class UtilTests(unittest.TestCase):
    def test_natural_order(self):
        names = ["lesson 10.mp4", "lesson 2.mp4", "lesson 1.mp4"]
        self.assertEqual(
            sorted(names, key=natural_key),
            ["lesson 1.mp4", "lesson 2.mp4", "lesson 10.mp4"],
        )

    def test_hms(self):
        self.assertEqual(hms(65), "1:05")
        self.assertEqual(hms(3725), "1:02:05")

    def test_slugify(self):
        self.assertEqual(slugify("03 - Modeling: The Mug!"), "03-modeling-the-mug")

    def test_title_from_filename(self):
        from pathlib import Path

        self.assertEqual(title_from_filename(Path("03 - the_mug.mp4")), "the mug")


if __name__ == "__main__":
    unittest.main()
