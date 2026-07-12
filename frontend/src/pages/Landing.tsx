import { Link } from "react-router-dom";
import { ArrowRight } from "lucide-react";
import { Logo } from "../components/Logo";
import { ThemeToggle } from "../components/ThemeToggle";
import { ProductPreview } from "../components/ProductPreview";

const STEPS = [
  {
    n: "01",
    title: "Say what you want to learn",
    body: "A short back-and-forth turns a rough idea into a scoped goal before any lesson begins.",
  },
  {
    n: "02",
    title: "Practice what you actually don't know",
    body: "Every answer updates a per-topic mastery estimate — the tutor spends time where it counts.",
  },
  {
    n: "03",
    title: "Review right before you'd forget",
    body: "Spaced repetition schedules reviews at the point retention would otherwise drop.",
  },
];

export function Landing() {
  return (
    <div className="bg-base-100 min-h-svh">
      <header className="mx-auto flex h-16 max-w-[1280px] items-center justify-between px-6">
        <Logo />
        <ThemeToggle />
      </header>

      <section className="mx-auto grid max-w-[1280px] grid-cols-1 items-center gap-16 px-6 py-20 lg:grid-cols-[55%_1fr]">
        <div className="flex flex-col items-start gap-6">
          <span className="text-primary border-primary/30 rounded-field text-caption border px-3 py-1">
            Adaptive tutoring that remembers what you've learned
          </span>
          <h1 className="text-display text-base-content">Learning that actually sticks.</h1>
          <p className="text-body text-base-content/70 max-w-md">
            Guru pairs spaced practice with an AI tutor that tracks exactly what you know — so you
            spend less time relearning and more time moving forward.
          </p>
          <Link to="/app/chat" className="btn btn-primary">
            Get started
            <ArrowRight size={16} />
          </Link>
        </div>
        <ProductPreview />
      </section>

      <section className="border-base-300 border-t">
        <div className="mx-auto max-w-[1280px] px-6 py-20">
          <h2 className="text-h2 mb-12">How it works</h2>
          <div className="flex flex-col">
            {STEPS.map((step, i) => (
              <div
                key={step.n}
                className={`flex items-start gap-8 py-8 ${i > 0 ? "border-base-300 border-t" : ""}`}
              >
                <span className="text-display text-primary/25 w-24 shrink-0">{step.n}</span>
                <div>
                  <h3 className="text-h3 mb-2">{step.title}</h3>
                  <p className="text-body text-base-content/70 max-w-lg">{step.body}</p>
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>
    </div>
  );
}
