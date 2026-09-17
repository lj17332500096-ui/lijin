# claude-api

## 用途
Reference for the Claude API / Anthropic SDK — model ids, pricing, params, streaming, tool use, MCP, agents, caching, token counting, model migration.
TRIGGER — read BEFORE opening the target file; don't skip because it "looks like a one-liner" — whenever: the prompt names Claude/Anthropic in any form (Claude, Anthropic, Fable, Opus, Sonnet, Haiku, `anthropic`, `@anthropic-ai`, `claude-*`, `us.anthropic.*`, `[1m]`); the user asks about an LLM (pricing/model choice/limits/caching) — never answer from memory; OR the task is LLM-shaped with provider unstated (agent/MCP/tool-definition/multi-agent/RAG/LLM-judge/computer-use; generate/summarize/extract/classify/rewrite/converse over NL; debugging refusals/cutoffs/streaming/tool-calls/tokens).
SKIP only when another provider is being worked on (overrides all triggers): OpenAI/GPT/Gemini/Llama/Mistral/Cohere/Ollama named in the query; OR `grep -rE 'openai|langchain_openai|google.generativeai|genai|mistralai|cohere|ollama'` over the project hits (run this grep FIRST if no provider named — don't Read the file).

## 触发
当用户请求与“claude-api”相关时，按下面的思路执行；若请求与本技能无关，直接正常回答，不要生搬硬套。

## 要点
（以下为自动提取的正文概览，需人工润色为可直接执行的中文步骤）

> # Building LLM-Powered Applications with Claude This skill helps you build LLM-powered applications with Claude. Choose the right surface based on your needs, detect the project language, then read the relevant language-specific documentation. ## Before You Start Scan the target file (or, if no target file, the prompt and project) for non-Anthropic provider markers - `import openai`, `from openai`, `langchain_openai`, `OpenAI(`, `gpt-4`, `gpt-5`, file names like `agent-openai.py` or `*-generic.py`, or any explicit instruction to keep the code provider-neutral. If you find any, stop and tell th

# 编译说明（自动生成）
- 来源技能：`claude-api`
- 本 skill.md 由 SKILL.md 的 description + 正文结构**脚手架化**生成（未做语义级翻译润色）。
- 若需更贴合本机「此刻/NOW」中文风格，请在发布前人工润色正文，或直接替换为人工中文稿。
- 资源（templates/fonts/references）原样拷贝到 assets/；本机 skills_loader 只读 skill.md，
  不自动托管这些资源，用到时需在 skill.md 里说明如何找到它们。