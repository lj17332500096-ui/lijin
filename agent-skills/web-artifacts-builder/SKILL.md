# web-artifacts-builder

## 用途
Suite of tools for creating elaborate, multi-component claude.ai HTML artifacts using modern frontend web technologies (React, Tailwind CSS, shadcn/ui). Use for complex artifacts requiring state management, routing, or shadcn/ui components - not for simple single-file HTML/JSX artifacts.

## 触发
当用户请求与“web-artifacts-builder”相关时，按下面的思路执行；若请求与本技能无关，直接正常回答，不要生搬硬套。

## 要点
（以下为自动提取的正文概览，需人工润色为可直接执行的中文步骤）

> # Web Artifacts Builder To build powerful frontend claude.ai artifacts, follow these steps: 1. Initialize the frontend repo using `scripts/init-artifact.sh` 2. Develop your artifact by editing the generated code 3. Bundle all code into a single HTML file using `scripts/bundle-artifact.sh` 4. Display artifact to user 5. (Optional) Test the artifact **Stack**: React 18 + TypeScript + Vite + Parcel (bundling) + Tailwind CSS + shadcn/ui ## Design & Style Guidelines VERY IMPORTANT: To avoid what is often referred to as "AI slop", avoid using excessive centered layouts, purple gradients, uniform rou

# 编译说明（自动生成）
- 来源技能：`web-artifacts-builder`
- 本 skill.md 由 SKILL.md 的 description + 正文结构**脚手架化**生成（未做语义级翻译润色）。
- 若需更贴合本机「此刻/NOW」中文风格，请在发布前人工润色正文，或直接替换为人工中文稿。
- 资源（templates/fonts/references）原样拷贝到 assets/；本机 skills_loader 只读 skill.md，
  不自动托管这些资源，用到时需在 skill.md 里说明如何找到它们。