---
name: academic-html-article
description: >
  Generate a single-file HTML article with a distinctive academic editorial design (white background,
  serif headings, KaTeX math, callout boxes, theorem blocks, SVG diagrams).
  Use this skill whenever the user asks to generate or export an HTML file during or after a learning
  conversation about a paper, project, or technical topic. Common triggers include:
  "生成HTML", "导出成HTML", "转成HTML", "帮我整理成HTML", "generate HTML", "export to HTML",
  "论文精读", "论文笔记", "教学讲义", "paper reading", "deep dive".
  Also trigger when the user uploads an existing file (markdown, docx, pdf, or a differently-styled HTML)
  and asks to convert or reformat it into this template style — e.g. "转成模板格式", "用这个风格重新排版",
  "reformat to template", "convert to this style".
  The output is a self-contained .html file with embedded CSS, KaTeX math, and optional inline SVG diagrams.
---

# Academic HTML Article Generator

Generate a **single self-contained HTML file** that looks like a professionally typeset academic tutorial.
The style is editorial — clean white background, warm accents, serif headings, generous whitespace.
It is designed primarily for Chinese-language content but works for any language.

## Workflow

This skill has two main entry paths. Identify which one applies before proceeding.

### Path A: Generate from Conversation Context

The user has been discussing a paper, project, or technical topic in the current chat, and now
asks to generate an HTML summary/article from what was discussed.

Steps:
1. Review the full conversation history to extract the key content: concepts explained, formulas
   derived, examples discussed, diagrams described, conclusions reached.
2. Organize the content into a logical article structure (see Content Structure Guidelines below).
3. Read `references/full-css.css` for the complete stylesheet.
4. Read `references/template.html` for the HTML boilerplate skeleton.
5. Generate the HTML file, filling in all sections with the extracted content.
6. Save to `/mnt/user-data/outputs/` and present to the user.

### Path B: Convert from Uploaded File

The user uploads an existing file (markdown, docx, pdf, or a differently-styled HTML) and asks
to convert it into this template's style.

Steps:
1. Read the uploaded file to extract its content. Use the appropriate method:
   - For `.md`, `.txt`, `.html`: read directly via `view` or `bash cat`.
   - For `.docx`: use the docx skill or `python-docx` to extract text.
   - For `.pdf`: use the pdf-reading skill to extract text.
2. Parse the content structure: identify title, sections, math formulas, tables, key points.
3. Map the content to the template's component library (sections → h2, key points → callout boxes,
   formulas → KaTeX, comparisons → tables, etc.).
4. Read `references/full-css.css` and `references/template.html`.
5. Generate the HTML file with the converted content.
6. Save to `/mnt/user-data/outputs/` and present to the user.

## Design System

### Fonts (loaded via Google Fonts)

| Role | Font | Fallback |
|------|------|----------|
| Headings | Libre Baskerville (700) | Noto Serif SC, Georgia, serif |
| Body | Source Sans 3 (400, 700) | Noto Sans SC, sans-serif |
| Mono (labels, code, algo) | IBM Plex Mono | monospace |

### Color Palette (CSS variables)

```css
:root {
    --bg: #ffffff;
    --bg-warm: #faf8f5;
    --bg-code: #f4f1ec;
    --ink: #1a1a1a;
    --ink-secondary: #4a4a4a;
    --ink-caption: #777777;
    --accent: #c0392b;        /* red — section numbers, danger boxes, keywords */
    --accent-light: #c0392b18;
    --teal: #16786a;           /* teal — success boxes, expert paths */
    --teal-light: #16786a15;
    --blue: #2c5aa0;           /* blue — links, info boxes */
    --blue-light: #2c5aa015;
    --border: #e0ddd8;
    --border-strong: #c8c4be;
}
```

### Layout

- Container: `max-width: 780px`, centered, `padding: 60px 28px 100px`
- Body: `font-size: 17px`, `line-height: 1.8`
- Responsive: at `max-width: 640px` reduce title size and padding

### Math Rendering

Use **KaTeX** loaded from CDN. Include these in `<head>`:

```html
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/contrib/auto-render.min.js"
    onload="renderMathInElement(document.body, {
        delimiters: [
            {left: '$$', right: '$$', display: true},
            {left: '$', right: '$', display: false}
        ],
        throwOnError: false
    });"></script>
```

Then write math directly in the HTML body using `$...$` for inline and `$$...$$` for display math.

## Component Library

### 1. Header

The header is flexible. Use whichever sub-components are relevant to the content.

**Full header (for paper deep-dives):**

```html
<header class="header">
    <div class="header-meta">CATEGORY · SUBTITLE TAG</div>
    <h1>Main Title<br>Can Span Two Lines</h1>
    <p class="header-subtitle">
        An engaging italic hook question or summary that draws the reader in.
    </p>
    <div class="paper-ref">
        <span class="paper-title">Full Paper Title in Italic</span><br>
        Authors · Venue Year · <a href="URL">arXiv:XXXX.XXXXX</a>
    </div>
</header>
```

**Minimal header (for project analysis, concept explainers, etc.):**

```html
<header class="header">
    <div class="header-meta">CATEGORY · TAG</div>
    <h1>Main Title</h1>
    <p class="header-subtitle">
        A brief description of what this article covers.
    </p>
</header>
```

- `header-meta`: IBM Plex Mono, 0.78rem, uppercase, letter-spacing 0.12em, caption color
- `header-subtitle`: Libre Baskerville italic, 1.15rem, secondary ink, max-width 640px
- `paper-ref`: OPTIONAL — only include when the content is about a specific paper. Warm background, left border 3px strong, contains italic paper title.

### 2. Table of Contents

```html
<nav class="toc">
    <div class="toc-label">目录</div>
    <ol>
        <li><a href="#s1">Section Title</a></li>
        <li><a href="#s2">Section Title</a></li>
    </ol>
</nav>
```

- Warm background, 1px border, custom counter with accent-colored mono numbers

### 3. Section Headings

```html
<section id="s1">
<h2><span class="section-num">1</span>Section Title</h2>
```

- h2: 1.55rem, bottom border 2px solid ink, margin-top 72px
- section-num: IBM Plex Mono, accent color, font-weight 600, margin-right 8px
- h3: 1.15rem, 700 weight, margin-top 40px (used for sub-topics within a section)

### 4. Callout Boxes

Four variants — default, danger (red), success (teal), info (blue):

```html
<div class="box danger">
    <div class="box-label">关键区别</div>
    <p>Content with <strong>bold</strong> for emphasis...</p>
</div>
```

- Left border 4px colored, tinted background
- box-label: IBM Plex Mono, 0.72rem, uppercase, letter-spacing, colored to match variant
- Variants: `.box.danger` (accent/red), `.box.success` (teal), `.box.info` (blue), plain `.box` (neutral)

**Usage guidance for box variants:**
- `danger` — critical distinctions, common mistakes, pitfalls, warnings
- `success` — key takeaways, summaries, positive results
- `info` — important context, clarifications, supplementary explanations
- plain (no class) — general notes, asides

### 5. Theorem / Proof Blocks

```html
<div class="theorem-block">
    <div class="theorem-label">THEOREM 2.1 (Name)</div>
    <p>Statement with $math$...</p>
</div>

<div class="theorem-block proof">
    <div class="theorem-label">PROOF SKETCH</div>
    <p>Proof content...</p>
</div>
```

- Theorem: 2px solid ink border, white bg
- Proof: 1px dashed border-strong, warm bg, label in secondary ink

### 6. Numbered Steps

```html
<ol class="steps">
    <li>First step description</li>
    <li>Second step description</li>
</ol>
```

- Custom counter with circular black badges (26px, white text)
- Warm background with border, left padding for badge space

### 7. Tables

```html
<table>
    <thead>
        <tr><th>Header</th><th>Header</th></tr>
    </thead>
    <tbody>
        <tr><td>Cell</td><td>Cell</td></tr>
    </tbody>
</table>
```

- Full width, collapsed borders, warm header background, 0.95rem

### 8. Inline Tags

```html
<span class="tag red">开环</span> Open-loop
<span class="tag green">闭环</span> Closed-loop
```

- Tiny mono uppercase label with colored border and tinted bg
- Good for labeling contrasting concepts side by side

### 9. Algorithm / Pseudocode Block

```html
<div class="algo">
    <div class="algo-label">ALGORITHM 1 — Name</div>
    <span class="kw">Initialize</span> dataset $\mathcal{D}$<br>
    <span class="kw">for</span> $i = 1$ to $N$: <span class="cm">// comment</span><br>
    &nbsp;&nbsp;...
</div>
```

- Code font, warm code background, keyword class `.kw` in accent red, comment class `.cm` in caption gray

### 10. Inline SVG Diagrams

For visual explanations, embed SVG directly:

```html
<svg viewBox="0 0 720 280" style="max-width: 680px;">
    <rect width="720" height="280" fill="#faf8f5" rx="4"/>
    <!-- Draw paths, circles, text labels using the color palette -->
</svg>
```

- Use the palette colors directly in SVG attributes
- Mono font for labels, sans-serif for descriptions
- Keep diagrams simple and educational

### 11. Footer

```html
<footer class="footer">
    <p>原论文：<a href="URL">Paper Title</a> · Venue Year</p>
    <p style="margin-top: 6px;">教学讲义 · Topic Description</p>
</footer>
```

- Adapt footer text to match the content type. For non-paper content, omit the paper link
  and use a general description like "技术笔记 · Topic" or "项目分析 · Project Name".

## Content Structure Guidelines

The structure should be adapted to the content type. Here are common patterns:

### Paper Deep-Dive (论文精读)

1. **Header** — category tag, provocative title, subtitle hook, paper reference
2. **TOC** — numbered sections
3. **Background / Prerequisites** — set up concepts the reader needs
4. **Core Problem** — explain with intuition first, then formalize
5. **Formal Treatment** — theorems, proofs, step-by-step derivation
6. **Solution / Method** — algorithm blocks, worked examples
7. **Practical Implications** — bridge to engineering practice
8. **Summary** — `.box.success` with key takeaways
9. **Footer** — paper link

### Project / Codebase Analysis (项目分析)

1. **Header** — project name, what it does, link to repo
2. **TOC**
3. **Overview** — what the project solves, architecture diagram (SVG)
4. **Core Mechanisms** — explain key modules with code snippets and diagrams
5. **Design Decisions** — why certain approaches were chosen (use comparison tables)
6. **Limitations & Extensions** — callout boxes for known issues, future work
7. **Summary**
8. **Footer**

### Concept Explainer (概念讲解)

1. **Header** — concept name as title, subtitle hook
2. **TOC**
3. **Intuition** — analogies, diagrams
4. **Formal Definition** — math, theorem blocks
5. **Examples** — worked examples with steps
6. **Common Mistakes** — danger callout boxes
7. **Connections** — how this relates to other concepts
8. **Summary**
9. **Footer**

### Writing Style

- **Bilingual fluency**: Chinese body text with English technical terms inline
- **Progressive depth**: intuition → visual → formal math → practical takeaway
- **Analogies**: use vivid real-world analogies to ground abstract concepts
- **Questions as hooks**: use rhetorical questions to drive curiosity
- **Bold for emphasis**: highlight key terms with `<strong>`
- **Math as language**: use KaTeX inline math naturally within sentences
- **Choose components wisely**: not every article needs every component. Use theorem blocks
  only when there are actual theorems; use algorithm blocks only for pseudocode; use SVG
  diagrams only when a visual genuinely helps understanding.

## Full CSS Reference

Include this complete `<style>` block in the generated HTML file. Read the reference file for the full CSS:

→ See `references/full-css.css` for the complete stylesheet to embed.

## Output Requirements

1. Generate a **single .html file** with all CSS inlined in a `<style>` tag
2. Include KaTeX CDN links and auto-render script in `<head>`
3. Include Google Fonts links for Libre Baskerville, Source Sans 3, IBM Plex Mono, Noto Sans SC, Noto Serif SC
4. Write math using `$...$` and `$$...$$` — KaTeX auto-render handles it
5. Save the file to `/mnt/user-data/outputs/` with a descriptive filename
6. The file must be fully self-contained and viewable offline (except for CDN fonts/KaTeX)
