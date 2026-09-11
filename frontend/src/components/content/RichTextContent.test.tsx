import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import RichText from "./RichTextContent";
import type { Citation } from "../../api/sse";

const cite = (marker: number): Citation => ({
  marker,
  chunk_id: `chunk-${marker}`,
  source_id: `source-${marker}`,
});

describe("structure the old plain-text renderer could not show", () => {
  it("renders a heading, a list, and a table as structure rather than literal syntax", () => {
    const { container } = render(
      <RichText
        content={"## Eigenvalues\n\n- first\n- second\n\n| a | b |\n| - | - |\n| 1 | 2 |"}
      />,
    );
    expect(screen.getByRole("heading", { name: "Eigenvalues" })).toBeInTheDocument();
    expect(container.querySelectorAll("li")).toHaveLength(2);
    expect(container.querySelector("table td")?.textContent).toBe("1");
    expect(container.textContent).not.toContain("##");
  });

  it("renders a linear-algebra derivation as typeset math, inline and display", () => {
    const { container } = render(
      <RichText
        content={"Since $A\\mathbf{v} = \\lambda\\mathbf{v}$:\n\n$$\\det(A - \\lambda I) = 0$$"}
      />,
    );
    expect(container.querySelectorAll(".katex")).toHaveLength(2);
    expect(container.querySelector(".katex-display")).not.toBeNull();
    // KaTeX emits MathML alongside the visual arm; that is what a screen reader reads.
    expect(container.querySelector("math")).not.toBeNull();
    // The TeX source survives only in KaTeX's hidden annotation; the arm that is actually
    // painted must show typeset symbols, not backslashes.
    const visible = [...container.querySelectorAll(".katex-html")].map((e) => e.textContent);
    expect(visible).toHaveLength(2);
    expect(visible.join("")).not.toContain("\\lambda");
    expect(visible.join("")).toContain("λ");
  });

  it("keeps a code sample verbatim instead of reading it as Markdown", () => {
    const { container } = render(
      <RichText content={"```python\nrows = [r for r in m if r[0] > 1]\n_total_ = 0\n```"} />,
    );
    const code = container.querySelector("pre code");
    expect(code?.textContent).toBe("rows = [r for r in m if r[0] > 1]\n_total_ = 0\n");
    // `_total_` would be emphasis in prose; inside a fence it is an identifier.
    expect(container.querySelector("pre em")).toBeNull();
  });

  it("does not render raw HTML from model output or uploaded material", () => {
    const { container } = render(<RichText content={'<img src="x" onerror="alert(1)">done'} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain("<img");
  });
});

describe("display math", () => {
  it("centres an equation written alone on its line", () => {
    // How a model actually writes a standalone equation. remark-math alone reads this as
    // inline math, so it landed cramped inside a paragraph.
    const { container } = render(<RichText content={"$$\\det(A - \\lambda I) = 0$$"} />);
    expect(container.querySelector(".katex-display")).not.toBeNull();
  });

  it("still renders an equation used mid-sentence inline", () => {
    const { container } = render(<RichText content={"We need $$x$$ here, not on its own."} />);
    expect(container.querySelector(".katex")).not.toBeNull();
    expect(container.querySelector(".katex-display")).toBeNull();
  });

  it("leaves the explicitly fenced form alone", () => {
    const { container } = render(<RichText content={"$$\nAx = b\n$$"} />);
    expect(container.querySelector(".katex-display")).not.toBeNull();
  });
});

describe("LaTeX's other delimiters", () => {
  it("typesets \\(x\\) as inline math", () => {
    const { container } = render(<RichText content={"A \\(2\\times2\\) matrix."} />);
    expect(container.querySelector(".katex")).not.toBeNull();
    expect(container.querySelector(".katex-display")).toBeNull();
    expect(container.querySelector(".katex-html")?.textContent).toBe("2×2");
  });

  it("typesets \\[x\\] alone on its line as display math", () => {
    const { container } = render(<RichText content={"\\[\\det(A) = 0\\]"} />);
    expect(container.querySelector(".katex-display")).not.toBeNull();
  });

  it("leaves the delimiters alone inside code", () => {
    const { container } = render(<RichText content={"```tex\n\\(x\\)\n```"} />);
    expect(container.querySelector(".katex")).toBeNull();
    expect(container.querySelector("pre code")?.textContent).toBe("\\(x\\)\n");
  });

  it("keeps the surrounding sentence intact", () => {
    const { container } = render(<RichText content={"before \\(x\\) after"} />);
    expect(container.querySelector("p")?.textContent).toContain("before");
    expect(container.querySelector("p")?.textContent).toContain("after");
  });
});

describe("citations", () => {
  it("turns a known marker into a control that opens that citation", async () => {
    const onCitationClick = vi.fn();
    render(
      <RichText
        content="Rank is preserved [1]."
        citations={[cite(1)]}
        onCitationClick={onCitationClick}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /citation 1/ }));
    expect(onCitationClick).toHaveBeenCalledWith(cite(1));
  });

  it("keeps the marker inside the paragraph it belongs to", () => {
    const { container } = render(
      <RichText
        content="A claim [1] and its consequence."
        citations={[cite(1)]}
        onCitationClick={vi.fn()}
      />,
    );
    // The old renderer split the raw string, so a marker mid-sentence broke the block apart.
    expect(container.querySelectorAll("p")).toHaveLength(1);
    expect(container.querySelector("p")?.textContent).toBe("A claim 1 and its consequence.");
  });

  it("leaves a marker we hold no citation for as plain text", () => {
    render(<RichText content="Invented [7]." citations={[cite(1)]} onCitationClick={vi.fn()} />);
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByText(/Invented \[7\]\./)).toBeInTheDocument();
  });

  it("leaves the marker exactly as written when the page cannot act on one", () => {
    // The guided-practice page passed a no-op handler, so markers looked live and did nothing.
    const { container } = render(<RichText content="A claim [1]." citations={[cite(1)]} />);
    expect(screen.queryByRole("button")).toBeNull();
    expect(container.querySelector("p")?.textContent).toBe("A claim [1].");
  });

  it("does not mistake an array index in code for a citation", () => {
    const { container } = render(
      <RichText
        content={"```python\nfirst = row[1]\n```"}
        citations={[cite(1)]}
        onCitationClick={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button")).toBeNull();
    expect(container.querySelector("pre code")?.textContent).toBe("first = row[1]\n");
  });

  it("does not mistake a subscript in math for a citation", () => {
    const { container } = render(
      <RichText content={"$$x_{[1]} + y$$"} citations={[cite(1)]} onCitationClick={vi.fn()} />,
    );
    expect(screen.queryByRole("button")).toBeNull();
    // KaTeX replaces its own children, so "no button" alone would still pass with the subscript
    // silently rewritten to x_{1}. The annotation carries the TeX that was actually typeset.
    expect(container.querySelector("annotation")?.textContent).toBe("x_{[1]} + y");
  });

  it("does not mistake Markdown link syntax for a citation", () => {
    render(
      <RichText
        content="See [1](https://example.com/paper)."
        citations={[cite(1)]}
        onCitationClick={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button")).toBeNull();
    const link = screen.getByRole("link", { name: "1" });
    expect(link).toHaveAttribute("href", "https://example.com/paper");
    expect(link).toHaveAttribute("rel", expect.stringContaining("noopener"));
  });
});
