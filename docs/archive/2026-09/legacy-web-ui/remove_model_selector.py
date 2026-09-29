from pathlib import Path
import re
p=Path('web/runtime/workspace.js');s=p.read_text(encoding='utf-8')
s=s.replace(', model_pref: composerModelPref()', '').replace('model_pref: composerModelPref(), ','')
s=re.sub(r'^\s*renderComposerModel\(\);\n','\n',s,flags=re.M)
a=s.index('  function composerModelPref()');b=s.index('  /* ---------- 对话流智能滚动',a);s=s[:a]+s[b:]
a=s.index('    var cmodel = $("composerModel");');b=s.index('    var ci0 =',a);s=s[:a]+s[b:]
s=s.replace('foot.insertBefore(folder, $("composerModel"));','foot.insertBefore(folder, $("sendBtn"));')
a=s.index('      var secModel = el(');b=s.index('      var sec4 =',a);s=s[:a]+s[b:]
p.write_text(s,encoding='utf-8')
p=Path('web/runtime.html');s=p.read_text(encoding='utf-8');s=re.sub(r'\s*<select class="composer-model".*?</select>','',s,flags=re.S);p.write_text(s.replace('v=225','v=226'),encoding='utf-8')
p=Path('web/runtime/design.css');s=p.read_text(encoding='utf-8');s=re.sub(r'\.composer-model\s*\{[^}]*\}\s*','',s);s=s.replace('.send,.composer-file-btn,','.send,.composer-file-btn,');p.write_text(s,encoding='utf-8')
p=Path('runtime/runner.py');s=p.read_text(encoding='utf-8');s=s.replace('按 项目设置 > env 默认 > gateway','仅按后台 env 配置 > gateway；忽略历史项目偏好');s=s.replace('model_pref = str((proj or {}).get("model_pref") or "").strip() or \\\n                os.getenv("FORGE_MODEL_PREF", "").strip() or "gateway"','model_pref = os.getenv("FORGE_MODEL_PREF", "").strip().lower() or "gateway"');p.write_text(s,encoding='utf-8')
p=Path('webapp.py');s=p.read_text(encoding='utf-8');s=s.replace('work_location_id=body.get("work_location_id"),\n                                 model_pref=body.get("model_pref"))','work_location_id=body.get("work_location_id"))');s=s.replace('        model_pref=body.get("model_pref"),\n','');p.write_text(s,encoding='utf-8')
p=Path('tests/test_model_select.py');s=p.read_text(encoding='utf-8').replace('默认不改行为；本地经项目 model_pref 生效。','后台配置决定模型，历史项目 model_pref 不得覆盖后台。');s=s.replace('test_project_local_pref_selects_local_model_and_provider','test_stale_project_local_cannot_override_backend_gateway').replace('test_project_gateway_overrides_env_local','test_stale_project_gateway_cannot_override_backend_local')
a=s.index('    def test_stale_project_local');b=s.index('    def test_project_local_without',a);block=s[a:b].replace('self.assertEqual(h.captured["agent_model"], "Ornith-Local")','self.assertEqual(h.captured["agent_model"], os.getenv("AGENT_MODEL"))').replace('self.assertIsNotNone(h.captured["provider"])','self.assertIsNone(h.captured["provider"])');s=s[:a]+block+s[b:]
a=s.index('    def test_stale_project_gateway');s=s[:a]+s[a:].replace('self.assertEqual(h.captured["provider"], None)','self.assertIsNotNone(h.captured["provider"])').replace('self.assertEqual(h.captured["agent_model"], os.getenv("AGENT_MODEL"))','self.assertEqual(h.captured["agent_model"], "Ornith-Local")');p.write_text(s,encoding='utf-8')
p=Path('tests/frontend_smoke.js');s=p.read_text(encoding='utf-8');s=re.sub(r'ok\("model selector present.*?;','ok("model selection managed by backend", await page.locator("#composerModel").count() === 0);',s);p.write_text(s,encoding='utf-8')
