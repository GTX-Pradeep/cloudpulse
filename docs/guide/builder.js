// Generic block -> docx builder for the CloudPulse guide.
const fs = require("fs");
const path = require("path");
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, AlignmentType, Table, TableRow,
  TableCell, WidthType, ShadingType, BorderStyle, ImageRun, PageBreak, TableOfContents,
  Footer, Header, PageNumber, LevelFormat, TabStopType,
} = require("docx");

const REPO = path.resolve(__dirname, "..", "..");
const IMG = path.join(__dirname, "img");
const PAGE_W = 11906, MARGIN = 1134; // A4, 2 cm margins
const CONTENT_W = PAGE_W - 2 * MARGIN; // 9638 DXA
const FONT = "Calibri", MONO = "Consolas";
const C = { ink: "1D1F23", muted: "5F6670", blue: "2F5BD3", line: "D9DCE1", code: "F3F4F6",
  head: "E8EDFB", good: "1F7A4D", bad: "B4232C", warn: "9A6400", noteBg: "FFF8E6", tipBg: "EAF4EE" };

// ---------- inline markup: **bold**, `code`, _italic_ ----------
function runs(text, base = {}) {
  const out = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`|\*[^*\s][^*]*\*)/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(new TextRun({ text: text.slice(last, m.index), ...base }));
    const tok = m[0];
    if (tok.startsWith("**")) out.push(new TextRun({ text: tok.slice(2, -2), bold: true, ...base }));
    else if (tok.startsWith("`")) out.push(new TextRun({ text: tok.slice(1, -1), font: MONO, size: (base.size || 21) - 2, color: "2B3A67", ...base, bold: base.bold }));
    else out.push(new TextRun({ text: tok.slice(1, -1), italics: true, ...base })); // *italic*
    last = m.index + tok.length;
  }
  if (last < text.length) out.push(new TextRun({ text: text.slice(last), ...base }));
  return out;
}

const border = { style: BorderStyle.SINGLE, size: 4, color: C.line };
const borders = { top: border, bottom: border, left: border, right: border };

function cell(text, width, opts = {}) {
  const paras = String(text).split("\n").map((t) => new Paragraph({
    spacing: { before: 20, after: 20 },
    children: runs(t, { size: opts.size || 18, bold: opts.bold, color: opts.color }),
  }));
  return new TableCell({
    width: { size: width, type: WidthType.DXA }, borders,
    shading: opts.fill ? { fill: opts.fill, type: ShadingType.CLEAR, color: "auto" } : undefined,
    margins: { top: 60, bottom: 60, left: 100, right: 100 },
    children: paras,
  });
}

function table(headers, rows, widths) {
  const total = widths.reduce((a, b) => a + b, 0);
  const scale = CONTENT_W / total;
  const w = widths.map((x) => Math.floor(x * scale));
  w[w.length - 1] += CONTENT_W - w.reduce((a, b) => a + b, 0);
  return new Table({
    width: { size: CONTENT_W, type: WidthType.DXA }, columnWidths: w,
    rows: [
      new TableRow({ tableHeader: true, children: headers.map((h, i) => cell(h, w[i], { bold: true, fill: C.head })) }),
      ...rows.map((r) => new TableRow({ children: r.map((v, i) => cell(v, w[i])) })),
    ],
  });
}

function codeBlock(text, caption) {
  const lines = text.replace(/\t/g, "    ").split("\n");
  const out = [];
  if (caption) out.push(new Paragraph({ spacing: { before: 120, after: 40 }, children: runs(caption, { size: 18, color: C.muted, italics: true }) }));
  lines.forEach((l, i) => out.push(new Paragraph({
    shading: { fill: C.code, type: ShadingType.CLEAR, color: "auto" },
    spacing: { before: i === 0 ? 40 : 0, after: i === lines.length - 1 ? 140 : 0, line: 250 },
    indent: { left: 120, right: 120 },
    children: [new TextRun({ text: l.length ? l : " ", font: MONO, size: 16, color: "1D2433" })],
  })));
  return out;
}

function callout(kind, text) {
  const fill = kind === "tip" ? C.tipBg : C.noteBg;
  const label = { tip: "Interview angle: ", note: "Note: ", warn: "Honest limitation: ", key: "Key idea: " }[kind] || "";
  return [new Table({
    width: { size: CONTENT_W, type: WidthType.DXA }, columnWidths: [CONTENT_W],
    rows: [new TableRow({ children: [new TableCell({
      width: { size: CONTENT_W, type: WidthType.DXA },
      borders: { top: { style: BorderStyle.NONE }, bottom: { style: BorderStyle.NONE }, right: { style: BorderStyle.NONE },
        left: { style: BorderStyle.SINGLE, size: 24, color: kind === "tip" ? C.good : kind === "warn" ? C.bad : C.warn } },
      shading: { fill, type: ShadingType.CLEAR, color: "auto" },
      margins: { top: 100, bottom: 100, left: 180, right: 140 },
      children: [new Paragraph({ children: [new TextRun({ text: label, bold: true, size: 20 }), ...runs(text, { size: 20 })] })],
    })] })],
  }), new Paragraph({ spacing: { after: 80 }, children: [] })];
}

function image(file, widthPx, caption) {
  const p = path.isAbsolute(file) ? file : path.join(IMG, file);
  const data = fs.readFileSync(p);
  // read PNG size
  const w = data.readUInt32BE(16), h = data.readUInt32BE(20);
  const maxW = widthPx || 640;
  const width = Math.min(maxW, 640);
  const height = Math.round((h / w) * width);
  const capped = height > 860 ? { width: Math.round(width * 860 / height), height: 860 } : { width, height };
  const out = [new Paragraph({ alignment: AlignmentType.CENTER, spacing: { before: 120, after: 40 },
    children: [new ImageRun({ type: "png", data, transformation: capped,
      altText: { title: caption || file, description: caption || file, name: path.basename(file) } })] })];
  if (caption) out.push(new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 200 },
    children: runs(caption, { size: 18, italics: true, color: C.muted }) }));
  return out;
}

// ---------- extract real code from the repo ----------
function pyDef(file, name) {
  const src = fs.readFileSync(path.join(REPO, file), "utf8").split("\n");
  const start = src.findIndex((l) => new RegExp(`^(\\s*)(async )?(def|class) ${name}\\b`).test(l));
  if (start < 0) throw new Error(`${name} not found in ${file}`);
  const indent = src[start].match(/^\s*/)[0].length;
  let end = start + 1;
  while (end < src.length) {
    const l = src[end];
    if (l.trim() && l.match(/^\s*/)[0].length <= indent && !l.trim().startsWith(")")) break;
    end++;
  }
  while (end > start && !src[end - 1].trim()) end--;
  return src.slice(start, end).join("\n");
}
function fileText(file, from, to) {
  const s = fs.readFileSync(path.join(REPO, file), "utf8");
  if (from === undefined) return s.trimEnd();
  const i = s.indexOf(from);
  if (i < 0) throw new Error(`marker not found in ${file}: ${from}`);
  const j = to ? s.indexOf(to, i + from.length) : -1;
  return s.slice(i, j < 0 ? undefined : j + (to ? to.length : 0)).trimEnd();
}

// ---------- blocks -> docx children ----------
function render(blocks) {
  const out = [];
  for (const b of blocks) {
    switch (b.t) {
      case "h1": out.push(new Paragraph({ heading: HeadingLevel.HEADING_1, pageBreakBefore: !b.noBreak, children: [new TextRun(b.text)] })); break;
      case "h2": out.push(new Paragraph({ heading: HeadingLevel.HEADING_2, children: [new TextRun(b.text)] })); break;
      case "h3": out.push(new Paragraph({ heading: HeadingLevel.HEADING_3, children: [new TextRun(b.text)] })); break;
      case "p": out.push(new Paragraph({ spacing: { after: 120, line: 276 }, children: runs(b.text) })); break;
      case "bullets": for (const it of b.items) out.push(new Paragraph({ numbering: { reference: "bullets", level: it.startsWith("  ") ? 1 : 0 }, spacing: { after: 60, line: 264 }, children: runs(it.trim()) })); break;
      case "steps": b.items.forEach((it) => out.push(new Paragraph({ numbering: { reference: b.ref || "steps", level: 0 }, spacing: { after: 60, line: 264 }, children: runs(it) }))); break;
      case "code": out.push(...codeBlock(b.text, b.caption)); break;
      case "table": out.push(table(b.headers, b.rows, b.widths), new Paragraph({ spacing: { after: 160 }, children: [] })); break;
      case "img": out.push(...image(b.file, b.width, b.caption)); break;
      case "callout": out.push(...callout(b.kind, b.text)); break;
      case "qa":
        b.items.forEach(([q, a], i) => {
          out.push(new Paragraph({ spacing: { before: 140, after: 40 }, keepNext: true, children: [new TextRun({ text: `Q${b.start ? b.start + i : i + 1}. `, bold: true, color: C.blue }), ...runs(q, { bold: true })] }));
          out.push(new Paragraph({ spacing: { after: 80, line: 264 }, indent: { left: 360 }, children: runs(a) }));
        });
        break;
      case "break": out.push(new Paragraph({ children: [new PageBreak()] })); break;
      default: throw new Error("unknown block " + b.t);
    }
  }
  return out;
}

function build(title, subtitle, coverLines, blocks, outFile) {
  const cover = [
    new Paragraph({ spacing: { before: 2400 }, children: [new TextRun({ text: title, bold: true, size: 72, color: C.blue })] }),
    new Paragraph({ spacing: { before: 200, after: 600 }, children: [new TextRun({ text: subtitle, size: 32, color: C.muted })] }),
    ...coverLines.map((l) => new Paragraph({ spacing: { after: 80 }, children: runs(l, { size: 22 }) })),
    new Paragraph({ children: [new PageBreak()] }),
    new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun("Contents")] }),
    new TableOfContents("Contents", { hyperlink: true, headingStyleRange: "1-2" }),
  ];
  const doc = new Document({
    creator: "CloudPulse", title, description: subtitle, features: { updateFields: true },
    styles: {
      default: { document: { run: { font: FONT, size: 21, color: C.ink } } },
      paragraphStyles: [
        { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
          run: { size: 36, bold: true, color: C.blue, font: FONT }, paragraph: { spacing: { before: 240, after: 200 }, outlineLevel: 0 } },
        { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
          run: { size: 28, bold: true, color: C.ink, font: FONT }, paragraph: { spacing: { before: 300, after: 120 }, outlineLevel: 1, keepNext: true } },
        { id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true,
          run: { size: 23, bold: true, color: "2B3A67", font: FONT }, paragraph: { spacing: { before: 200, after: 80 }, outlineLevel: 2, keepNext: true } },
      ],
    },
    numbering: { config: [
      { reference: "bullets", levels: [
        { level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 540, hanging: 260 } } } },
        { level: 1, format: LevelFormat.BULLET, text: "–", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 1000, hanging: 260 } } } } ] },
      ...Array.from({ length: 30 }, (_, k) => ({ reference: k === 0 ? "steps" : `steps${k}`, levels: [
        { level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 540, hanging: 300 } } } } ] })),
    ] },
    sections: [{
      properties: { page: { size: { width: PAGE_W, height: 16838 }, margin: { top: MARGIN, bottom: MARGIN, left: MARGIN, right: MARGIN } } },
      headers: { default: new Header({ children: [new Paragraph({ alignment: AlignmentType.RIGHT, children: [new TextRun({ text: "CloudPulse — complete technical guide", size: 16, color: C.muted })] })] }) },
      footers: { default: new Footer({ children: [new Paragraph({ alignment: AlignmentType.CENTER, children: [new TextRun({ children: [PageNumber.CURRENT], size: 16, color: C.muted })] })] }) },
      children: [...cover, ...render(blocks)],
    }],
  });
  return Packer.toBuffer(doc).then((buf) => fs.writeFileSync(outFile, buf));
}

module.exports = { build, pyDef, fileText, REPO };
