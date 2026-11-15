/**
 * "The problem" slide. On arrival the 1960 debate studio photo fills the section; scrolling on fades
 * it out and the paragraph rises in. Pinned + scrubbed with GSAP ScrollTrigger. ↓ / → play the
 * reveal first (one press), then move on to the next slide; ↑ / ← go back to the photo first.
 */
import { useGSAP } from "@gsap/react";
import gsap from "gsap";
import { ScrollTrigger } from "gsap/ScrollTrigger";
import { useEffect, useRef } from "react";

gsap.registerPlugin(useGSAP, ScrollTrigger);

export function Problem() {
  const root = useRef<HTMLElement>(null);
  const stRef = useRef<ScrollTrigger | null>(null);
  // Set once ↓ has asked for the reveal: a second ↓ while it is still playing moves on instead of
  // replaying it (a presenter pressing quickly must never feel stuck).
  const revealAsked = useRef(false);

  useGSAP(
    () => {
      const mm = gsap.matchMedia();
      mm.add("(prefers-reduced-motion: no-preference)", () => {
        const tl = gsap.timeline({ defaults: { ease: "power2.inOut" } });
        tl.to({}, { duration: 0.35 }) // the photo holds for a moment first
          .to(".problem-photo", { opacity: 0, scale: 1.06, duration: 0.4 }, 0.35)
          .to(".problem-hint", { opacity: 0, duration: 0.15 }, 0.35)
          .fromTo(".problem-reveal", { opacity: 0, y: 40 }, { opacity: 1, y: 0, duration: 0.35, stagger: 0.12, ease: "power2.out" }, 0.55)
          .to({}, { duration: 0.15 }); // hold the text before the pin releases
        stRef.current = ScrollTrigger.create({
          trigger: root.current,
          start: "top 48px", // under the sticky nav
          end: "+=150%",
          pin: true,
          scrub: 0.6,
          animation: tl,
          onUpdate: (self) => {
            if (self.progress < 0.03) revealAsked.current = false; // back at the photo (keys or mouse)
          },
        });
      });
      mm.add("(prefers-reduced-motion: reduce)", () => {
        gsap.set(".problem-photo, .problem-hint", { opacity: 0 });
        gsap.set(".problem-reveal", { opacity: 1, y: 0 });
      });
    },
    { scope: root },
  );

  // Capture phase, so this runs before the page's slide navigation and can hold it back.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const st = stRef.current;
      const r = root.current?.getBoundingClientRect();
      const mid = window.innerHeight / 2;
      if (!st || !r || r.top > mid || r.bottom < mid) return;
      if (e.target instanceof HTMLElement && e.target.closest("input, textarea, select")) return;
      const fwd = ["ArrowDown", "PageDown", " ", "ArrowRight"].includes(e.key);
      const back = ["ArrowUp", "PageUp", "ArrowLeft"].includes(e.key);
      const p = st.progress;
      let to: number | null = null;
      if (fwd && p < 0.97 && !revealAsked.current) {
        to = st.end - 2; // reveal the paragraph
        revealAsked.current = true;
      } else if (back && p > 0.03) {
        to = st.start; // back to the photo
        revealAsked.current = false;
      } else if (fwd && p < 0.97) {
        // second ↓ during the reveal: jump to the next slide
        document.querySelector('[data-slide="2"]')?.scrollIntoView({ behavior: "smooth" });
        e.preventDefault();
        e.stopImmediatePropagation();
        return;
      }
      else if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
        // ← / → are not slide keys elsewhere on the page: move to the neighbouring slide here.
        document.querySelector(`[data-slide="${e.key === "ArrowRight" ? 2 : 0}"]`)?.scrollIntoView({ behavior: "smooth" });
        e.preventDefault();
        e.stopImmediatePropagation();
        return;
      }
      if (to == null) return;
      e.preventDefault();
      e.stopImmediatePropagation();
      window.scrollTo({ top: to, behavior: "smooth" });
    };
    window.addEventListener("keydown", onKey, { capture: true });
    return () => window.removeEventListener("keydown", onKey, { capture: true });
  }, []);

  return (
    <section ref={root} data-slide={1} className="relative overflow-hidden bg-almond text-ink">
      <div className="mx-auto grid h-[calc(100dvh-3rem)] max-w-6xl items-center gap-12 px-6 py-10 lg:grid-cols-2">
        <figure>
          <img
            src="/img/first-televised-debate.jpg"
            alt="Kennedy and Nixon shake hands in the television studio before the first televised presidential debate, 1960"
            className="max-h-[55vh] w-full rounded-2xl object-cover shadow-xl shadow-ink/20 grayscale"
          />
          <figcaption className="mt-3 text-sm text-ink/55">Chicago, September 26, 1960. The first televised presidential debate.</figcaption>
        </figure>
        <div>
          <p className="eyebrow text-mahogany">The problem</p>
          <h2 className="display mt-3 text-5xl md:text-7xl">Debates went live in 1960. Fact-checking still hasn't.</h2>
          <p className="problem-reveal lead mt-8">
            Tens of millions of people watch a presidential debate as it happens. The fact-checks arrive afterwards, in articles most of
            that audience never reads. By then the line has landed and the moment has passed.
          </p>
          <p className="problem-reveal lead mt-5">
            BS Subtitles moves the check into the broadcast itself: the same captions viewers already read, marked where a claim does not
            hold up.
          </p>
        </div>
      </div>

      {/* The studio photo covers the slide on arrival, then fades out as you scroll. */}
      <div className="problem-photo pointer-events-none absolute inset-0 origin-center">
        <img
          src="/img/jfk-nixon-debate.jpg"
          alt="A museum recreation of the 1960 debate studio; the screen reads First Televised Presidential Debate, September 26, 1960"
          className="h-full w-full object-cover"
        />
      </div>
      <p className="problem-hint pointer-events-none absolute inset-x-0 bottom-6 text-center text-sm font-medium text-cream [text-shadow:0_1px_8px_rgba(2,9,30,0.8)]">
        Scroll or press ↓
      </p>
    </section>
  );
}
