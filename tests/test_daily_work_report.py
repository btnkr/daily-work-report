from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import daily_report as collector
from daily_report import save_report


class DailyWorkReportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "projects"
        self.workspace.mkdir()
        self.config_path = self.root / "config.json"
        self.options = {
            "output_dir": str(self.root / "reports"),
            "codex_home": str(self.root / "codex"),
            "claude_home": str(self.root / "claude"),
            "project_roots": [],
        }
        self.configure()

    def configure(self, **options):
        self.options.update(options)
        self.config_path.write_text(json.dumps(self.options), encoding="utf-8")
        self.config = collector.load_config(str(self.config_path))

    def write_jsonl(self, path, records, append=False):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a" if append else "w", encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    def codex(self, records, session_id="session-a", directory="sessions", metadata=None):
        path = self.config["codex_home"] / directory / "2026/10/20" / f"rollout-{session_id}.jsonl"
        meta = {"id": session_id, "cwd": str(self.workspace), "timestamp": "2026-10-19T23:00:00Z"}
        meta.update(metadata or {})
        self.write_jsonl(path, [{"type": "session_meta", "payload": meta}, *records])
        return path

    def message(self, text="修复订单筛选", at="2026-10-20T01:00:00Z", role="user", kind="event"):
        if kind == "event":
            return {"timestamp": at, "type": "event_msg", "payload": {
                "type": "user_message" if role == "user" else "agent_message", "message": text}}
        return {"timestamp": at, "type": "response_item", "payload": {
            "type": "message", "role": role, "content": [{"type": "text", "text": text}]}}

    def payload(self, date="2026-10-20"):
        return collector.build_payload(self.config, date)

    def events(self, payload):
        sessions = [session for stream in payload["workstreams"] for session in stream["sessions"]]
        return [message for session in [*sessions, *payload["unattached_sessions"]] for message in session["events"]]

    def git(self, repo, *arguments, env=None):
        environment = dict(os.environ)
        environment.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        environment.update(env or {})
        return subprocess.run(["git", "-C", str(repo), *arguments], check=True, capture_output=True,
                              text=True, env=environment).stdout.strip()

    def repository(self):
        repo = self.workspace / "orders"
        repo.mkdir()
        self.git(repo, "init")
        self.git(repo, "config", "user.name", "Report User")
        self.git(repo, "config", "user.email", "me@example.test")
        return repo

    def commit(self, repo, name, author="me@example.test", at="2026-10-20T09:00:00+08:00"):
        target = repo / name
        target.write_text(name, encoding="utf-8")
        self.git(repo, "add", name)
        return self.git(repo, "commit", "-m", name, env={
            "GIT_AUTHOR_NAME": "Report User", "GIT_AUTHOR_EMAIL": author,
            "GIT_AUTHOR_DATE": at, "GIT_COMMITTER_DATE": at,
        })

    def test_duplicate_representations_prefer_event_and_retain_source(self):
        self.codex([self.message(kind="response"), self.message(),
                    self.message("已提交，尚未测试", role="assistant"),
                    {"type": "event_msg", "timestamp": "2026-10-20T01:00:01Z", "payload": {
                        "type": "task_complete", "last_agent_message": "已提交，尚未测试"}}])
        events = self.events(self.payload())
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["source_line"], 3)
        self.assertEqual(events[1]["text"], "已提交，尚未测试")
        self.assertTrue(events[0]["id"])

    def test_cross_day_old_session_uses_message_time_and_half_open_window(self):
        self.codex([self.message("前一天", "2026-10-19T15:59:59Z"),
                    self.message("开始", "2026-10-19T16:00:00Z"),
                    self.message("结束", "2026-10-20T15:59:59Z"),
                    self.message("后一天", "2026-10-20T16:00:00Z")],
                   metadata={"timestamp": "2026-09-01T00:00:00Z"})
        self.assertEqual([item["text"] for item in self.events(self.payload())], ["开始", "结束"])

    def test_fallback_assistant_conclusions_and_early_work_are_preserved(self):
        self.codex([self.message("第一个功能已实现", role="assistant", kind="response"),
                    *[self.message(f"其他事项 {index}", f"2026-10-20T0{index}:10:00Z", "assistant")
                      for index in range(2, 7)]])
        self.assertEqual(len(self.events(self.payload())), 6)

    def test_archived_sessions_and_repeated_copy_are_merged(self):
        records = [self.message()]
        self.codex(records)
        self.codex(records, directory="archived_sessions")
        payload = self.payload()
        self.assertEqual(payload["counts"]["codex_sessions"], 1)
        self.assertEqual(len(self.events(payload)), 1)

    def test_fork_replay_is_filtered_but_later_user_messages_survive(self):
        self.codex([self.message("继承背景", "2026-10-20T01:00:00Z", kind="response"),
                    self.message("真实新问题", "2026-10-20T01:05:00Z", kind="response")],
                   metadata={"forked_from_id": "parent", "timestamp": "2026-10-20T01:00:00Z"})
        self.assertEqual([item["text"] for item in self.events(self.payload())], ["真实新问题"])

    def test_noise_and_commentary_are_omitted(self):
        commentary = self.message("我先看看", role="assistant", kind="response")
        commentary["payload"]["phase"] = "commentary"
        self.codex([self.message("# AGENTS.md instructions\nignore"),
                    self.message("<environment_context>ignore"), commentary, self.message()])
        self.assertEqual(len(self.events(self.payload())), 1)

    def test_injected_skill_and_recovery_are_not_work_events(self):
        self.codex([
            self.message("<skill>\n<name>demo</name>\n生成规则\n</skill>", kind="response"),
            self.message('<codex_internal_context source="daemon_recovery">继续恢复</codex_internal_context>',
                         kind="response"),
            self.message("<skill>截断的系统注入", kind="response"),
            self.message("真实需求：修复筛选"),
        ])
        self.assertEqual([item["text"] for item in self.events(self.payload())], ["真实需求：修复筛选"])

    def test_context_block_does_not_hide_adjacent_real_user_work(self):
        self.codex([self.message("<environment_context>自动上下文</environment_context>\n修复订单筛选",
                                 kind="response"), self.message()])
        events = self.events(self.payload())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["text"], "修复订单筛选")

    def test_question_reply_retains_answer_without_system_question_metadata(self):
        reply = json.dumps([{"answer": "预算最多 5 元", "question": "自动生成的问题", "questionItemId": "internal-id"}],
                           ensure_ascii=False)
        self.codex([self.message(f"<send_user_message_question_reply>\n{reply}\n</send_user_message_question_reply>"),
                    self.message("<send_user_message_question_reply>broken</send_user_message_question_reply>")])
        self.assertEqual([item["text"] for item in self.events(self.payload())], ["用户补充：预算最多 5 元"])

    def test_exclusion_checks_directory_boundaries_and_turn_context(self):
        private = self.workspace / "private"
        self.configure(exclude_dirs=[str(private)])
        self.codex([self.message(), {"type": "turn_context", "payload": {"cwd": str(private / "child")}}])
        self.assertEqual(self.payload()["counts"]["codex_sessions"], 0)
        self.assertTrue(collector.allowed_path(str(self.workspace / "private-public"), self.config))

    def test_explicit_session_exclusion(self):
        self.codex([self.message()])
        payload = collector.build_payload(self.config, "2026-10-20", ["session-a"])
        self.assertEqual(payload["counts"]["codex_sessions"], 0)

    def test_claude_history_fallback_keeps_all_questions(self):
        path = self.config["claude_home"] / "history.jsonl"
        timestamp = datetime(2026, 10, 20, 1, tzinfo=timezone.utc).timestamp() * 1000
        self.write_jsonl(path, [{"sessionId": "history", "project": str(self.workspace),
                                 "timestamp": timestamp + index * 60000, "display": f"问题 {index}"}
                                for index in range(3)])
        payload = self.payload()
        self.assertEqual(payload["counts"]["claude_sessions"], 1)
        self.assertEqual(len(self.events(payload)), 3)

    def test_claude_full_session_supersedes_history_and_ignores_sidechain(self):
        root = self.config["claude_home"]
        self.write_jsonl(root / "projects/project/session.jsonl", [
            {"sessionId": "session", "cwd": str(self.workspace), "timestamp": "2026-10-20T01:00:00Z",
             "type": "user", "message": {"content": "正文问题"}},
            {"sessionId": "session", "cwd": str(self.workspace), "timestamp": "2026-10-20T01:01:00Z",
             "type": "assistant", "isSidechain": True, "message": {"content": "子任务重复"}}])
        self.write_jsonl(root / "history.jsonl", [{"sessionId": "session", "project": str(self.workspace),
                                                  "timestamp": 1792458000000, "display": "历史重复"}])
        self.assertEqual([item["text"] for item in self.events(self.payload())], ["正文问题"])

    def test_claude_excluded_directory_cannot_return_via_history(self):
        private = self.workspace / "private"
        self.configure(exclude_dirs=[str(private)])
        root = self.config["claude_home"]
        self.write_jsonl(root / "projects/project/session.jsonl", [
            {"sessionId": "session", "cwd": str(private), "timestamp": "2026-10-20T01:00:00Z",
             "type": "user", "message": {"content": "私密问题"}}])
        self.write_jsonl(root / "history.jsonl", [{"sessionId": "session", "project": str(self.workspace),
                                                  "timestamp": 1792458000000, "display": "不应恢复"}])
        self.assertEqual(self.payload()["counts"]["claude_sessions"], 0)

    def test_redaction_and_truncation_do_not_merge_distinct_long_messages(self):
        self.configure(max_text_chars=20)
        self.codex([self.message("x" * 25 + "first"), self.message("x" * 25 + "second"),
                    self.message("sk-proj-" + "a" * 30, "2026-10-20T02:00:00Z")])
        events = self.events(self.payload())
        self.assertEqual(len(events), 3)
        self.assertTrue(events[0]["truncated"])
        self.assertNotEqual(events[0]["id"], events[1]["id"])
        self.assertEqual(events[2]["text"], "[REDACTED]")

    def test_same_session_append_changes_digest_but_other_day_does_not(self):
        path = self.codex([self.message()])
        original = self.payload()["evidence_digest"]
        self.assertEqual(original, self.payload()["evidence_digest"])
        self.write_jsonl(path, [self.message("下一天", "2026-10-21T01:00:00Z")], append=True)
        self.assertEqual(original, self.payload()["evidence_digest"])
        self.write_jsonl(path, [self.message("当天新进展", "2026-10-20T02:00:00Z")], append=True)
        self.assertNotEqual(original, self.payload()["evidence_digest"])

    def test_git_uses_local_identity_and_exact_email_without_future_midnight(self):
        repo = self.repository()
        self.commit(repo, "mine")
        self.commit(repo, "other", author="other-me@example.test")
        self.commit(repo, "tomorrow", at="2026-10-21T00:00:00+08:00")
        self.configure(project_roots=[str(self.workspace)])
        info = self.payload()["git_only"][0]
        self.assertEqual([item["subject"] for item in info["commits"]], ["mine"])

    def test_missing_author_omits_commits_instead_of_including_everyone(self):
        repo = self.repository()
        self.commit(repo, "mine")
        self.git(repo, "config", "--unset", "user.email")
        start, end = collector.day_bounds("2026-10-20", self.config["tz"])
        warnings = []
        original = os.environ.copy()
        try:
            os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
            os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
            info = collector.collect_git(repo, self.config, start, end, warnings)
        finally:
            os.environ.clear()
            os.environ.update(original)
        self.assertEqual(info["commits"], [])
        self.assertTrue(any("No author email" in warning for warning in warnings))

    def test_historical_working_tree_is_explicitly_current_and_read_only(self):
        repo = self.repository()
        self.commit(repo, "mine")
        (repo / "mine").write_text("uncommitted\n", encoding="utf-8")
        before = self.git(repo, "diff")
        self.configure(project_roots=[str(self.workspace)])
        start, end = collector.day_bounds("2026-10-20", self.config["tz"])
        info = collector.collect_git(repo, self.config, start, end, [])
        self.assertFalse(info["working_tree"]["historical"])
        self.assertTrue(info["working_tree"]["observed_at"])
        self.assertTrue(info["working_tree"]["status_porcelain"])
        self.assertEqual(before, self.git(repo, "diff"))

    def test_working_tree_changes_only_update_snapshot_digest(self):
        repo = self.repository()
        self.commit(repo, "mine")
        self.codex([self.message()], metadata={"cwd": str(repo)})
        original = self.payload()
        save_report(self.config["output_dir"], original, "# 历史日报\n已提交筛选调整")
        (repo / "mine").write_text("现在的改动\n", encoding="utf-8")
        updated = self.payload()
        self.assertEqual(original["evidence_digest"], updated["evidence_digest"])
        self.assertNotEqual(original["snapshot_digest"], updated["snapshot_digest"])
        self.assertEqual(collector.report_status(self.config["output_dir"], updated["date"], "brief",
                                                updated["evidence_digest"]), "up_to_date")

    def test_historical_new_commit_still_updates_evidence_digest(self):
        repo = self.repository()
        self.commit(repo, "mine")
        self.codex([self.message()], metadata={"cwd": str(repo)})
        original = self.payload()
        self.commit(repo, "another-target-day-commit")
        self.assertNotEqual(original["evidence_digest"], self.payload()["evidence_digest"])

    def test_git_project_in_temporary_directory_keeps_alias_and_session_link(self):
        repo = self.repository()
        self.commit(repo, "mine")
        self.configure(project_names={str(repo): "订单后台"})
        self.codex([self.message()], metadata={"cwd": str(repo)})
        payload = self.payload()
        self.assertEqual(payload["workstreams"][0]["name"], "订单后台")
        self.assertEqual(payload["workstreams"][0]["git"]["commits"][0]["subject"], "mine")
        self.assertEqual(payload["git_only"], [])
        self.assertEqual(payload["unattached_sessions"], [])

    def test_out_of_order_git_dates_do_not_hide_target_day(self):
        repo = self.repository()
        self.commit(repo, "target-day")
        self.commit(repo, "backdated-head", at="2026-10-01T00:00:00+08:00")
        start, end = collector.day_bounds("2026-10-20", self.config["tz"])
        info = collector.collect_git(repo, self.config, start, end, [])
        self.assertEqual([commit["subject"] for commit in info["commits"]], ["target-day"])

    def test_dst_calendar_day_has_correct_elapsed_hours(self):
        start, end = collector.day_bounds("2026-11-01", ZoneInfo("America/New_York"))
        self.assertEqual((end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds(), 25 * 3600)

    def test_corrupt_record_warns_but_valid_evidence_survives(self):
        path = self.codex([self.message()])
        with path.open("a", encoding="utf-8") as stream:
            stream.write("{truncated\n")
        payload = self.payload()
        self.assertEqual(len(self.events(payload)), 1)
        self.assertTrue(any("Invalid JSON" in warning for warning in payload["warnings"]))

    def test_save_creates_report_then_skips_same_evidence(self):
        evidence = {"date": "2026-10-20", "evidence_digest": "initial"}
        first = save_report(self.config["output_dir"], evidence, "# 日报\n原文")
        second = save_report(self.config["output_dir"], evidence, "# 日报\n不同写法")
        self.assertEqual(first["status"], "saved")
        self.assertEqual(second["status"], "up_to_date")
        self.assertIn("原文", Path(first["path"]).read_text(encoding="utf-8"))

    def test_manual_edits_and_previous_drafts_are_preserved(self):
        evidence = {"date": "2026-10-20", "evidence_digest": "initial"}
        saved = save_report(self.config["output_dir"], evidence, "原文")
        target = Path(saved["path"])
        target.write_text("人工补充", encoding="utf-8")
        draft = save_report(self.config["output_dir"], evidence, "新稿")
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(target.read_text(encoding="utf-8"), "人工补充")
        draft_path = Path(draft["path"])
        draft_path.write_text("草稿也修改了", encoding="utf-8")
        with self.assertRaises(ValueError):
            save_report(self.config["output_dir"], evidence, "新稿")
        self.assertEqual(draft_path.read_text(encoding="utf-8"), "草稿也修改了")

    def test_new_evidence_creates_draft_and_explicit_replace_updates_state(self):
        evidence = {"date": "2026-10-20", "evidence_digest": "initial"}
        save_report(self.config["output_dir"], evidence, "旧稿")
        evidence["evidence_digest"] = "updated"
        draft = save_report(self.config["output_dir"], evidence, "新稿")
        self.assertEqual(draft["status"], "draft")
        saved = save_report(self.config["output_dir"], evidence, "新稿", replace=True)
        self.assertEqual(saved["status"], "saved")
        self.assertEqual(collector.report_status(self.config["output_dir"], evidence["date"], "brief", "updated"), "up_to_date")

    def test_identical_report_can_acknowledge_new_evidence(self):
        evidence = {"date": "2026-10-20", "evidence_digest": "initial"}
        save_report(self.config["output_dir"], evidence, "内容不变")
        evidence["evidence_digest"] = "updated"
        result = save_report(self.config["output_dir"], evidence, "内容不变")
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(collector.report_status(self.config["output_dir"], evidence["date"], "brief", "updated"), "up_to_date")

    def test_brief_and_detailed_have_independent_files(self):
        evidence = {"date": "2026-10-20", "evidence_digest": "initial"}
        detailed = save_report(self.config["output_dir"], evidence, "详细", mode="detailed")
        brief = save_report(self.config["output_dir"], evidence, "简短", mode="brief")
        self.assertNotEqual(detailed["path"], brief["path"])

    def test_cli_collect_preview_and_save_end_to_end(self):
        self.codex([self.message()])
        command = [sys.executable, str(SCRIPTS / "daily_report.py"), "collect", "--config", str(self.config_path),
                   "--date", "2026-10-20"]
        preview = subprocess.run([*command, "--print"], check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(preview.stdout)["counts"]["codex_sessions"], 1)
        self.assertFalse(self.config["output_dir"].exists())
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["report_status"], "missing")
        candidate = self.root / "candidate.md"
        candidate.write_text("# 日报\n合成验证", encoding="utf-8")
        saved = subprocess.run([sys.executable, str(SCRIPTS / "daily_report.py"), "save", "--config", str(self.config_path),
                                "--evidence", summary["evidence_file"], "--candidate", str(candidate)],
                               check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(saved.stdout)["status"], "saved")
        again = subprocess.run(command, check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(again.stdout)["report_status"], "up_to_date")

    def test_cli_output_override_applies_to_evidence_report_and_status(self):
        self.codex([self.message()])
        output_dir = self.root / "chosen output"
        command = [sys.executable, str(SCRIPTS / "daily_report.py"), "collect", "--config", str(self.config_path),
                   "--date", "2026-10-20", "--output-dir", str(output_dir), "--mode", "brief"]
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        summary = json.loads(result.stdout)
        self.assertEqual(Path(summary["evidence_file"]), output_dir / "_raw/2026-10-20.json")
        candidate = self.root / "candidate.md"
        candidate.write_text("# 简短日报", encoding="utf-8")
        result = subprocess.run([sys.executable, str(SCRIPTS / "daily_report.py"), "save", "--config", str(self.config_path),
                                 "--output-dir", str(output_dir), "--mode", "brief",
                                 "--evidence", summary["evidence_file"], "--candidate", str(candidate)],
                                check=True, capture_output=True, text=True)
        saved = json.loads(result.stdout)
        self.assertEqual(Path(saved["path"]), output_dir / "daily/2026-10-20.brief.md")
        self.assertFalse(self.config["output_dir"].exists())
        again = subprocess.run(command, check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(again.stdout)["report_status"], "up_to_date")

    def test_preview_with_output_override_writes_nothing(self):
        self.codex([self.message()])
        output_dir = self.root / "preview-only"
        result = subprocess.run([sys.executable, str(SCRIPTS / "daily_report.py"), "collect",
                                 "--config", str(self.config_path), "--date", "2026-10-20",
                                 "--output-dir", str(output_dir), "--print"],
                                check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout)["schema_version"], 2)
        self.assertFalse(output_dir.exists())

    def test_save_infers_output_from_evidence_when_working_directory_changes(self):
        self.codex([self.message()])
        command = [sys.executable, str(SCRIPTS / "daily_report.py"), "collect", "--config", str(self.config_path),
                   "--date", "2026-10-20"]
        collected = json.loads(subprocess.run(command, check=True, capture_output=True, text=True).stdout)
        candidate = self.root / "candidate.md"
        candidate.write_text("# 日报\n已验证", encoding="utf-8")
        result = subprocess.run([sys.executable, str(SCRIPTS / "daily_report.py"), "save",
                                 "--evidence", collected["evidence_file"], "--candidate", str(candidate)],
                                cwd=self.workspace, check=True, capture_output=True, text=True)
        saved = json.loads(result.stdout)
        self.assertEqual(saved["path"], collected["report_file"])
        self.assertEqual(saved["status"], "saved")
        self.assertFalse((self.workspace / "daily-work-reports").exists())

    def test_default_output_directory_is_current_workspace(self):
        original = Path.cwd()
        try:
            os.chdir(self.workspace)
            self.config_path.write_text('{"timezone":"Asia/Shanghai"}', encoding="utf-8")
            config = collector.load_config(str(self.config_path))
        finally:
            os.chdir(original)
        self.assertEqual(config["output_dir"], self.workspace / "daily-work-reports")

    def test_invalid_configuration_and_missing_explicit_config_fail(self):
        self.config_path.write_text('{"scan_depth": 0}', encoding="utf-8")
        with self.assertRaises(ValueError):
            collector.load_config(str(self.config_path))
        with self.assertRaises(OSError):
            collector.load_config(str(self.root / "missing.json"))


if __name__ == "__main__":
    unittest.main()
