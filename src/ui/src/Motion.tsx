import { useState, type ReactNode } from "react";
import { motion, useMotionValueEvent, useReducedMotion, useScroll, useSpring } from "motion/react";

export const glide = { type: "spring" as const, stiffness: 380, damping: 34 };
export const easeOut = [0.22, 1, 0.36, 1] as const;

/** Scroll reveals keep the document's native scrolling and keyboard behavior. */
export function Reveal({ children, className, delay = 0 }: { children: ReactNode; className?: string; delay?: number }) {
  const reduced = useReducedMotion();
  return <motion.div className={className}
    initial={reduced ? false : { opacity: 0, y: 24 }}
    whileInView={{ opacity: 1, y: 0 }}
    viewport={{ once: true, amount: 0.08 }}
    transition={{ duration: reduced ? 0 : 0.6, ease: easeOut, delay: reduced ? 0 : delay }}>
    {children}
  </motion.div>;
}

export function ScrollTools() {
  const reduced = useReducedMotion();
  const { scrollY, scrollYProgress } = useScroll();
  const progress = useSpring(scrollYProgress, { stiffness: 180, damping: 32 });
  const [deep, setDeep] = useState(false);
  useMotionValueEvent(scrollY, "change", (y) => setDeep(y > 650));
  return <>
    {!reduced && <motion.div className="scroll-progress" aria-hidden="true" style={{ scaleX: progress }} />}
    {deep && <motion.button className="back-to-top" aria-label="Наверх"
      initial={reduced ? false : { opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }}
      onClick={() => window.scrollTo({ top: 0, behavior: reduced ? "instant" : "smooth" })}>↑</motion.button>}
  </>;
}
