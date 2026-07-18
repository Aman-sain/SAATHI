/* Skeleton canvas renderer (§7 P2b): draws [[x,y,conf]×17] COCO keypoints as a
 * stick figure. Phase 3 renders the embedded fixture frame; Phase 6 streams
 * real keypoints over /ws/dashboard (frames themselves never leave vision.py). */
"use strict";

const Skeleton = (() => {
  // COCO-17: 0 nose, 1-2 eyes, 3-4 ears, 5-6 shoulders, 7-8 elbows,
  // 9-10 wrists, 11-12 hips, 13-14 knees, 15-16 ankles
  const EDGES = [
    [0, 5], [0, 6], [5, 6], [5, 7], [7, 9], [6, 8], [8, 10],
    [5, 11], [6, 12], [11, 12], [11, 13], [13, 15], [12, 14], [14, 16],
  ];
  const MIN_CONF = 0.3;

  // one standing pose, normalized 0..1 — the Phase-3 fixture frame
  const FIXTURE = [
    [0.50, 0.10, 0.99], [0.48, 0.08, 0.95], [0.52, 0.08, 0.95],
    [0.46, 0.09, 0.90], [0.54, 0.09, 0.90], [0.42, 0.22, 0.98],
    [0.58, 0.22, 0.98], [0.38, 0.36, 0.95], [0.62, 0.36, 0.95],
    [0.36, 0.50, 0.93], [0.64, 0.50, 0.93], [0.45, 0.52, 0.97],
    [0.55, 0.52, 0.97], [0.44, 0.72, 0.95], [0.56, 0.72, 0.95],
    [0.44, 0.92, 0.92], [0.56, 0.92, 0.92],
  ];

  function draw(canvas, kp) {
    const ctx = canvas.getContext("2d");
    const w = canvas.width, h = canvas.height;
    ctx.clearRect(0, 0, w, h);

    // keypoints may be normalized (0..1) or pixel-space — scale accordingly
    const norm = kp.every((p) => p[0] <= 1.5 && p[1] <= 1.5);
    const sx = norm ? w : w / 640;
    const sy = norm ? h : h / 480;
    const pt = (p) => [p[0] * sx, p[1] * sy];

    ctx.strokeStyle = "#4aa3ff";
    ctx.lineWidth = 3;
    ctx.lineCap = "round";
    for (const [a, b] of EDGES) {
      if (kp[a][2] < MIN_CONF || kp[b][2] < MIN_CONF) continue;
      const [x1, y1] = pt(kp[a]);
      const [x2, y2] = pt(kp[b]);
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.stroke();
    }
    ctx.fillStyle = "#2ecc71";
    for (const p of kp) {
      if (p[2] < MIN_CONF) continue;
      const [x, y] = pt(p);
      ctx.beginPath();
      ctx.arc(x, y, 4, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  return { draw, FIXTURE };
})();
