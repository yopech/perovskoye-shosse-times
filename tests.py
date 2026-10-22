import copy
import hashlib
import imaplib
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import common
import gazette
from common import seal, write_json
from sources import calendar_event, compact_text, ingest_news


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "private"
        self.state.mkdir()
        self.issues = self.root / "issues"
        self.day = "2026-10-22"
        self.moment = datetime(2026,10,22,8,0,tzinfo=ZoneInfo("Europe/Moscow"))
        self.cfg = common.config()
        self.cfg.update(print_enabled=True,printer="Test_HP")
        self.patches = [patch.object(common,"STATE",self.state),patch.object(gazette,"STATE",self.state),
            patch.object(gazette,"WINDOWS",False),
            patch.object(gazette,"ISSUES",self.issues),patch.object(gazette,"now",return_value=self.moment),
            patch.object(gazette,"config",return_value=self.cfg)]
        for p in self.patches:p.start()

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()

    def ready(self, demo=False):
        folder=self.issues/self.day
        folder.mkdir(parents=True)
        pdf=folder/"газета.pdf"
        pdf.write_bytes(b"fake PDF used only by a mocked printer")
        d=common.day_dir(self.day)
        write_json(d/"packet.json",{"collected_at":self.moment.replace(hour=7,minute=40).isoformat()})
        write_json(d/"ready.json",{"date":self.day,"demo":demo,"pdf":str(pdf),"sha256":hashlib.sha256(pdf.read_bytes()).hexdigest()})
        return pdf,d

    def test_no_duplicate_submission(self):
        self.ready()
        with patch.object(gazette.subprocess,"run",return_value=SimpleNamespace(returncode=0,stdout="request id is Test_HP-42 (1 file(s))")) as run:
            self.assertEqual(gazette.print_tick()["state"],"submitted")
            self.assertEqual(gazette.print_tick()["state"],"submitted")
            self.assertEqual(run.call_count,1)
            self.assertEqual(run.call_args.args[0][1:3],["-d","Test_HP"])

    def test_timeout_is_not_retried(self):
        self.ready()
        with patch.object(gazette.subprocess,"run",side_effect=subprocess.TimeoutExpired("lp",30)) as run:
            self.assertEqual(gazette.print_tick()["state"],"uncertain_or_failed")
            gazette.print_tick()
            self.assertEqual(run.call_count,1)

    def test_localized_queue_receipt(self):
        self.ready()
        with patch.object(gazette.subprocess,"run",return_value=SimpleNamespace(returncode=0,
            stdout="идентификатор запроса: Test_HP-42 (1 файл)",stderr="")):
            self.assertEqual(gazette.print_tick()["job_id"],"Test_HP-42")

    def test_demo_never_auto_printed(self):
        self.ready(demo=True)
        with patch.object(gazette.subprocess,"run") as run:
            self.assertEqual(gazette.print_tick()["state"],"no_today_issue")
            run.assert_not_called()

    def test_changed_pdf_rejected(self):
        pdf,_=self.ready()
        pdf.write_bytes(b"changed")
        with patch.object(gazette.subprocess,"run") as run:
            with self.assertRaisesRegex(ValueError,"PDF изменён"):
                gazette.print_tick()
            run.assert_not_called()

    def test_stale_packet_rejected(self):
        _,d=self.ready()
        write_json(d/"packet.json",{"collected_at":"2026-10-21T07:40:00+03:00"})
        with patch.object(gazette.subprocess,"run") as run:
            self.assertEqual(gazette.print_tick()["state"],"stale_packet")
            run.assert_not_called()

    def test_no_print_before_eight(self):
        self.ready()
        with patch.object(gazette,"now",return_value=self.moment.replace(hour=7,minute=59)),patch.object(gazette.subprocess,"run") as run:
            self.assertEqual(gazette.print_tick()["state"],"outside_window")
            run.assert_not_called()

    def test_manual_print_after_daily_cutoff(self):
        self.ready()
        with patch.object(gazette,"now",return_value=self.moment.replace(minute=40)),patch.object(gazette.subprocess,"run",
            return_value=SimpleNamespace(returncode=0,stdout="request id is Test_HP-42 (1 file(s))")) as run:
            result=gazette.print_tick(manual=True)
            self.assertEqual(result["state"],"submitted")
            self.assertTrue(result["manual"])
            gazette.print_tick()
            gazette.print_tick(manual=True)
            self.assertEqual(run.call_count,1)

    def test_manual_print_requires_ready_issue(self):
        with patch.object(gazette.subprocess,"run") as run:
            self.assertEqual(gazette.print_tick(manual=True)["state"],"no_today_issue")
            run.assert_not_called()

    def test_cutoff_cancels_only_own_job(self):
        _,d=self.ready()
        write_json(d/"print.json",{"state":"submitted","job_id":"Test_HP-42"})
        with patch.object(gazette,"now",return_value=self.moment.replace(minute=30)),patch.object(gazette.subprocess,"run",
            side_effect=[SimpleNamespace(returncode=0,stdout="Test_HP-420 other\nTest_HP-42 user\n"),SimpleNamespace(returncode=0)]) as run:
            gazette.print_tick()
            self.assertEqual(run.call_args.args[0],["/usr/bin/cancel","Test_HP-42"])

    def test_naive_calendar_time_not_invented(self):
        result=calendar_event({"id":"1","summary":"All day?","start":"2026-10-22T00:00:00","end":"2026-10-23T00:00:00"})
        self.assertTrue(result["time_unspecified"])
        self.assertEqual(result["start"],"2026-10-22")
        self.assertEqual(result["original_start"],"2026-10-22T00:00:00")

    def valid_issue(self):
        packet=seal({"date":self.day,"sources":[{"id":"s1","kind":"mail"}],"events":[]})
        issue={"date":self.day,"packet_sha256":packet["packet_sha256"],"demo":False,"editorial_line":"Тест"}
        issue.update({k:[] for k in gazette.SECTIONS})
        issue["essentials"]=[{"headline":"Дедлайн","body":"До 23:59","source_ids":["s1"],"priority":5,"event_key":""}]
        return issue,packet

    def test_fabricated_source_rejected(self):
        issue,packet=self.valid_issue()
        issue["essentials"][0]["source_ids"]=["invented"]
        with self.assertRaisesRegex(ValueError,"отсутствующий источник"):
            gazette.validate_issue(issue,packet)

    def test_modified_packet_rejected(self):
        issue,packet=self.valid_issue()
        packet["events"].append({"start":self.day})
        with self.assertRaisesRegex(ValueError,"Пакет изменён"):
            gazette.validate_issue(issue,packet)

    def test_mail_is_not_accepted_as_news(self):
        issue,packet=self.valid_issue()
        issue["news"]=issue["essentials"]
        with self.assertRaisesRegex(ValueError,"проверенные публикации"):
            gazette.validate_issue(issue,packet)

    def test_app_password_visual_spaces_removed(self):
        self.assertEqual(gazette.normalize_app_password(" abcd efgh\u00a0ijkl mnop \n"),"abcdefghijklmnop")

    def test_email_padding_does_not_hide_deadline(self):
        text="\n\n    \n\n\u034f\u200b   Дедлайн:\u00a0 23 октября 16:00\n\n\n\n"
        self.assertEqual(compact_text(text),"Дедлайн: 23 октября 16:00")

    def test_auth_diagnostic_contains_no_credentials(self):
        error=imaplib.IMAP4.error("[AUTHENTICATIONFAILED] login failed user@example.com secret123")
        message=gazette.yandex_error_message(error,"login")
        self.assertIn("AUTHENTICATIONFAILED",message)
        self.assertNotIn("secret123",message)
        self.assertNotIn("user@example.com",message)

    def test_disabled_imap_diagnostic(self):
        message=gazette.yandex_error_message(imaplib.IMAP4.error("IMAP is disabled"),"login")
        self.assertIn("IMAP отключён",message)

    def test_cannot_overwrite_submitted_edition(self):
        _,d=self.ready()
        write_json(d/"print.json",{"state":"submitted","job_id":"Test_HP-1"})
        with self.assertRaisesRegex(ValueError,"Перезапись остановлена"):
            gazette.publish(self.day,d/"does-not-exist.json")

    def test_fallback_label_and_no_cursor_advance(self):
        packet=seal({"date":self.day,"collected_at":self.moment.replace(hour=7,minute=40).isoformat(),
            "events":[],"sources":[{"id":"t1","kind":"telegram","title":"Чат","chat":"Чат","text":"Пример исходного сообщения","url":""}],
            "meme":None,"warnings":[],"status":{"calendar":{"state":"not_connected","detail":"Нет доступа"},
                "telegram":{"state":"ok","detail":"Прочитано"}},"telegram_watermarks":{"1":200}})
        d=common.day_dir(self.day)
        write_json(d/"packet.json",packet)
        def fake_publish(day,path,**kwargs):
            issue=common.read_json(path)
            gazette.validate_issue(issue,packet)
            self.assertIn("без саммари",issue["editorial_line"])
            self.assertTrue(kwargs["fallback"])
            return {"ok":True}
        with patch.object(gazette,"_publish_unlocked",side_effect=fake_publish):
            self.assertTrue(gazette.fallback_unlocked(self.day)["ok"])
        self.assertFalse((self.state/"last-published.json").exists())


class MailLayoutTests(unittest.TestCase):
    def render_mail(self,accounts):
        from layout import render
        from pypdf import PdfReader
        packet={"date":"2026-10-21","collected_at":"2026-10-21T07:40:00+03:00","events":[],"meme":None,
            "sources":[],"status":{"calendar":{"state":"ok","detail":"Проверено"}},"warnings":[]}
        issue={"date":packet["date"],"demo":True,"editorial_line":"",**{key:[] for key in gazette.SECTIONS}}
        for i,account in enumerate(accounts):
            sid=f"mail:{i}"
            packet["sources"].append({"id":sid,"kind":"mail","account":account,"title":"Письмо","url":""})
            issue["mail"].append({"headline":"Документы","body":"Ответить до пятницы.","source_ids":[sid],"priority":3,"event_key":""})
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/"test.pdf"
            render(issue,packet,target)
            pdf=PdfReader(target)
            self.assertEqual(len(pdf.pages),1)
            return pdf.pages[0].extract_text().splitlines()

    def test_empty_personal_subsection_is_hidden(self):
        lines=self.render_mail(["corporate"])
        self.assertIn("Корпочта",lines)
        self.assertNotIn("Личная",lines)

    def test_empty_corporate_subsection_is_hidden(self):
        lines=self.render_mail(["personal"])
        self.assertIn("Личная",lines)
        self.assertNotIn("Корпочта",lines)

    def test_both_mailboxes_have_separate_subheadings(self):
        lines=self.render_mail(["corporate","personal"])
        self.assertEqual(lines.count("Корпочта"),1)
        self.assertEqual(lines.count("Личная"),1)

    def test_empty_mail_section_is_hidden(self):
        lines=self.render_mail([])
        self.assertFalse(any("РАЗГРЕБАЕМ ПОЧТУ" in line for line in lines))

    def test_cannot_merge_two_mailboxes_in_one_note(self):
        sources={"c":{"account":"corporate"},"p":{"account":"personal"}}
        with self.assertRaisesRegex(ValueError,"смешивает"):
            common.mail_groups([{"source_ids":["c","p"]}],sources)


if __name__=="__main__":unittest.main(verbosity=2)
