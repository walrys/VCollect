// Motion that behaves like a physical object. A spring is described the way Apple describes
// one: `response` (about how many seconds it takes to get there) and `damping` (1 settles
// without overshoot, lower bounces). It always starts from wherever the value is, keeping its
// velocity, so anything moving can be grabbed or sent somewhere else mid-flight.

export const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");

const STEP_S = 1 / 240; // small steps keep stiff springs stable

export class Spring {
  /** `precision`: how close counts as there, in the value's own units. */
  constructor(value, onChange, precision = 0.5) {
    this.value = value;
    this.velocity = 0;
    this.onChange = onChange;
    this.precision = precision;
    this.frame = 0;
    this.resolve = null;
  }

  /** Resolves true once it settles on `target`, false if something interrupts it first. */
  to(target, { response = 0.4, damping = 1, velocity = this.velocity } = {}) {
    this.finish(false);
    this.velocity = velocity;
    const stiffness = ((2 * Math.PI) / response) ** 2;
    const friction = (4 * Math.PI * damping) / response;
    return new Promise((resolve) => {
      this.resolve = resolve;
      let last = performance.now();
      const tick = (now) => {
        const elapsed = Math.min((now - last) / 1000, 1 / 30); // a hidden tab skips ahead
        last = now;
        const steps = Math.max(1, Math.ceil(elapsed / STEP_S));
        const dt = elapsed / steps;
        for (let i = 0; i < steps; i += 1) {
          const force = -stiffness * (this.value - target) - friction * this.velocity;
          this.velocity += force * dt;
          this.value += this.velocity * dt;
        }
        const resting =
          Math.abs(this.value - target) < this.precision &&
          Math.abs(this.velocity) < this.precision * 10;
        if (resting) {
          this.value = target;
          this.velocity = 0;
        }
        this.onChange(this.value);
        if (resting) {
          this.frame = 0;
          this.finish(true);
        } else {
          this.frame = requestAnimationFrame(tick);
        }
      };
      this.frame = requestAnimationFrame(tick);
    });
  }

  /** Jumps to `value`; pass a velocity to hand the motion on to the next to(). */
  set(value, velocity = 0) {
    this.finish(false);
    this.value = value;
    this.velocity = velocity;
    this.onChange(value);
  }

  /** Holds the value where it is now, e.g. when a finger catches it. */
  stop() {
    this.finish(false);
  }

  get moving() {
    return this.resolve !== null;
  }

  finish(settled) {
    cancelAnimationFrame(this.frame);
    this.frame = 0;
    const resolve = this.resolve;
    this.resolve = null;
    resolve?.(settled);
  }
}

/** How far a flick at `velocity` (px/s) carries before it stops, as a scroll view decelerates. */
export function project(velocity, decelerationRate = 0.998) {
  return ((velocity / 1000) * decelerationRate) / (1 - decelerationRate);
}

/** Past an edge, the further it is pulled the less it follows. */
export function rubberband(overshoot, dimension, constant = 0.55) {
  return (overshoot * dimension * constant) / (dimension + constant * Math.abs(overshoot));
}

/** Pointer velocity in px/s over the last few moves. */
export class VelocityTracker {
  constructor() {
    this.samples = [];
  }

  add(event) {
    const now = event.timeStamp;
    this.samples.push({ x: event.clientX, y: event.clientY, t: now });
    while (this.samples.length > 2 && now - this.samples[0].t > 100) this.samples.shift();
  }

  get velocity() {
    const first = this.samples[0];
    const last = this.samples.at(-1);
    const dt = (last?.t - first?.t) / 1000;
    if (!dt) return { x: 0, y: 0 };
    return { x: (last.x - first.x) / dt, y: (last.y - first.y) / dt };
  }
}
