import { Sprout } from "lucide-react";
import { MasteryRing } from "./MasteryRing";

/** A static mock of the real product (chat transcript + a mastery ring) for the landing
 * hero — showing the actual UI, not an abstract illustration standing in for it. */
export function ProductPreview() {
  return (
    <div className="bg-base-200 border-base-300 rounded-box border p-6">
      <div className="flex flex-col gap-5">
        <div className="flex gap-3">
          <span className="bg-primary/15 text-primary flex size-8 shrink-0 items-center justify-center rounded-full">
            <Sprout size={16} />
          </span>
          <p className="text-body text-base-content/90">
            Last time you mixed up <em>oxidation</em> and <em>reduction</em> — let's revisit that
            before moving on to electrochemical cells.
          </p>
        </div>
        <div className="flex justify-end">
          <p className="text-body text-base-content/70 max-w-[80%]">
            Right, oxidation is losing electrons. Can we try another example?
          </p>
        </div>
        <div className="flex gap-3">
          <span className="bg-primary/15 text-primary flex size-8 shrink-0 items-center justify-center rounded-full">
            <Sprout size={16} />
          </span>
          <p className="text-body text-base-content/90">
            Exactly. Here's one: in this reaction, which element is oxidized?
          </p>
        </div>
      </div>

      <div className="border-base-300 mt-6 flex items-center gap-4 border-t pt-5">
        <MasteryRing value={0.72} size={44} />
        <div>
          <p className="text-caption text-base-content/60">Chemistry mastery</p>
          <p className="text-h3">72%</p>
        </div>
      </div>
    </div>
  );
}
