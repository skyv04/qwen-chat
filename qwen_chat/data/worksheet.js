(async () => {
  const audit = document.getElementById("layout-audit");
  const config = JSON.parse(document.getElementById("worksheet-config").textContent);
  const pages = [];
  try {
    await Promise.all([
      document.fonts.load("400 16px QwenSans"),
      document.fonts.load("700 16px QwenSans"),
      ...config.icons.map(icon => document.fonts.load("80px QwenEmoji", icon))
    ]);
    await document.fonts.ready;

    // A loaded font can still draw nothing (notably COLRv1 with an incompatible
    // renderer). Inspect actual pixels rather than trusting the font's presence.
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = 150;
    const ctx = canvas.getContext("2d", {willReadFrequently: true});
    ctx.font = "80px QwenEmoji";
    for (const icon of config.icons) {
      ctx.clearRect(0, 0, 150, 150);
      ctx.fillText(icon, 8, 105);
      const pixels = ctx.getImageData(0, 0, 150, 150).data;
      let colored = 0;
      for (let i = 0; i < pixels.length; i += 4) {
        if (pixels[i + 3] &&
            Math.max(pixels[i], pixels[i + 1], pixels[i + 2]) -
            Math.min(pixels[i], pixels[i + 1], pixels[i + 2]) > 16) colored++;
      }
      if (colored < 8) throw new Error(
        `Emoji ${icon} is missing or not rendered in color. Choose another icon; no PDF was saved.`);
    }

    function newPage() {
      const page = document.getElementById("page-template").content.firstElementChild.cloneNode(true);
      if (!pages.length) page.classList.add("first-page");
      document.getElementById("document").append(page);
      pages.push(page);
      const content = page.querySelector(".content");
      if (content.getBoundingClientRect().height < 300) {
        throw new Error("The title/header leaves too little room. Use shorter display labels, not smaller body text.");
      }
      return content;
    }
    function fits(content) {
      const child = content.lastElementChild;
      return !child || child.getBoundingClientRect().bottom <= content.getBoundingClientRect().bottom + .5;
    }

    let content = newPage();
    const blocks = [...document.getElementById("blocks").children];
    for (const block of blocks) {
      content.append(block);
      if (!fits(content)) {
        block.remove();
        if (!content.children.length) {
          throw new Error(`Block ${block.dataset.block} is taller than a page. Split a long reading paragraph at a sentence boundary, or use a shorter label. Keep its wording.`);
        }
        content = newPage();
        content.append(block);
        if (!fits(content)) {
          throw new Error(`Block ${block.dataset.block} cannot fit intact on a page. Keep the question and its answer space together; review its requested line count.`);
        }
      }
    }
    if (pages.length > config.max_pages) {
      audit.textContent = JSON.stringify({
        ok: false, code: "page_limit", pages: pages.length, max_pages: config.max_pages,
        error: `The readable layout needs ${pages.length} pages; max_pages is ${config.max_pages}. Keep the content and writing space. Ask before increasing the page limit.`
      });
      return;
    }
    const answers = [];
    const fontSizes = [];
    for (const [i, page] of pages.entries()) {
      page.querySelector(".page-number").textContent = `Page ${i + 1} of ${pages.length}`;
      for (const el of page.querySelectorAll(".chunk, .hero-copy, .question-copy, .options")) {
        if (el.scrollWidth > el.clientWidth + 1) {
          throw new Error(`Text overflows horizontally on page ${i + 1}. No PDF was saved.`);
        }
      }
      for (const el of page.querySelectorAll(".reading-text, .q-prompt, .options li, .callout p")) {
        const style = getComputedStyle(el);
        fontSizes.push(parseFloat(style.fontSize) * .75);
        if (parseFloat(style.fontSize) < 14.66 ||
            parseFloat(style.lineHeight) < parseFloat(style.fontSize) * 1.3) {
          throw new Error("Body text is too small or tightly spaced.");
        }
      }
      for (const q of page.querySelectorAll(".question")) {
        const lines = q.querySelectorAll(".answer-line");
        if (lines.length !== Number(q.dataset.answerLines) ||
            (lines.length && q.querySelector(".answer-space").getBoundingClientRect().top <
              q.querySelector(".question-head").getBoundingClientRect().bottom)) {
          throw new Error(`Answer space overlaps question ${q.dataset.question}.`);
        }
        answers.push({question: Number(q.dataset.question), page: i + 1, lines: lines.length});
      }
    }
    if (document.querySelectorAll("#document [data-block]").length !== config.blocks) {
      throw new Error("A content block was lost during pagination.");
    }
    audit.textContent = JSON.stringify({
      ok: true, pages: pages.length, portrait: true, layout_checked: true,
      color_emoji_checked: config.icons.length,
      min_body_font_pt: Math.round(Math.min(...fontSizes) * 100) / 100, answers
    });
  } catch (error) {
    audit.textContent = JSON.stringify({ok: false, error: String(error.message || error)});
  }
})();
