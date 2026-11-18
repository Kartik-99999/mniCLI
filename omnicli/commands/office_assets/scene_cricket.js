/* OmniCLI Office - Cricket scene.
 *
 * A broadcast-style wide shot of a cricket ground. Each agent's pose picks a role on the field:
 *   working   -> batting at the striker's end (2nd batter at the non-striker's end, runs between the
 *                wickets every couple of deliveries; more batters pad up at the pavilion edge)
 *   reviewing -> bowling at the batter whose work is checked (other reviewers field close in)
 *   waiting   -> non-striker leaning on the bat, backing up (extra waiters field and walk in)
 *   free      -> padded up in the dugout (sips a drink, chats, claps the shots)
 *   blocked   -> at the boundary rope waving a red flag next to a "blocked" board
 *   complete  -> the team lines up by the pavilion, bats raised, confetti, scoreboard shows WON
 *
 * Signature moment: one delivery cycle (run-up -> arm over -> ball down the pitch -> shot -> ball
 * to the boundary). Every loop shares one clock (P seconds per delivery) so the bowler, the ball,
 * the batter, the backing-up non-striker, the walking-in fielders and the dugout claps stay in step.
 * Tuning lives in P, SC, RUN, G (positions) and SHOTS below.
 */
(function () {
  "use strict";

  var P = 5;            // seconds per delivery; every synced loop is a multiple of this
  var SC = 0.9;         // people scale on the ground
  var RUN = 200;        // bowler's run-up length (mark -> bowling crease)
  var G = {
    strike: [648, 368], ns: [390, 396], mark: [118, 368], umpire: [712, 300],
    close: [[724, 350], [600, 332], [606, 416]],
    field: [[482, 288], [566, 472], [826, 336], [806, 474], [214, 452]],
    pad: [[344, 226], [386, 229], [428, 232]],
    seatX: 52, seatDX: 56, seatY: 196,
    block: [[560, 196], [690, 205], [430, 194]],
    cheer: [[70, 292], [140, 298], [210, 302], [280, 302], [350, 298]],
    rel: [342, 292], bounce: [584, 370], hit: [621, 352]
  };
  // where the ball goes after the shot: m = mid point, e = end point, sm/se = shadow y on the ground
  var SHOTS = [
    { m: [470, 150], e: [330, 70], sm: 300, se: 214 },   // lofted straight, over the pavilion side
    { m: [540, 306], e: [452, 228], sm: 309, se: 231 },  // cover drive along the ground
    { m: [790, 300], e: [934, 272], sm: 303, se: 275 },  // pull
    { m: [770, 420], e: [934, 516], sm: 423, se: 519 }   // square cut
  ];
  var DEFEND = { m: [608, 364], e: [600, 370], sm: 368, se: 372 };
  // loop phase windows (fraction of P) in which a pose can start without its body jumping
  var WINDOW = { bowl: [0.4, 0.435], field: [0.2, 0.43], close: [0.2, 0.43], ns: [0, 0.56], nsbat: [0, 0.56],
                 strike: [0, 0.72], defend: [0, 0.72] };
  var POSE_CLS = { strike: ["ck-bat"], pad: ["ck-bat"], defend: ["ck-def"], ns: ["ck-ns"], nsbat: ["ck-ns"],
                   bowl: ["ck-bowl"], close: ["ck-field"], field: ["ck-field"], sit: ["ck-sit"],
                   block: ["ck-block"], cheer: ["ck-cheer"] };
  var ALL_POSE = ["ck-bat", "ck-def", "ck-ns", "ck-bowl", "ck-field", "ck-sit", "ck-sip", "ck-chat", "ck-clap",
                  "ck-block", "ck-cheer", "ck-seated", "ck-alt", "ck-ready"];
  var ROUTINES = ["ck-sip", "ck-chat", "ck-clap"];
  var POSE_COLOR = { working: "var(--working)", reviewing: "var(--reviewing)", waiting: "var(--waiting)",
                     free: "var(--free)", blocked: "var(--blocked)" };

  var st = null;  // per-mount state

  // ------------------------------------------------------------------ small helpers
  function now() { return performance.now(); }
  function phase() { return (((now() - st.T0) / 1000) % P) / P; }
  function clockDelay() { return (-(now() - st.T0) / 1000).toFixed(3) + "s"; }
  function cut(s, n) { s = String(s || ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; }
  function shade(hex, f) {
    if (!/^#[0-9a-f]{6}$/i.test(hex || "")) return hex || "#555";
    var n = parseInt(hex.slice(1), 16), r = n >> 16, g = (n >> 8) & 255, b = n & 255;
    function m(v) { return Math.round(f < 0 ? v * (1 + f) : v + (255 - v) * f); }
    return "#" + ((1 << 24) | (m(r) << 16) | (m(g) << 8) | m(b)).toString(16).slice(1);
  }
  function rng(seed) { return function () { seed = (seed * 16807) % 2147483647; return (seed - 1) / 2147483646; }; }

  // ------------------------------------------------------------------ keyframes (data -> CSS)
  var FN = {
    r: function (v) { return "rotate(" + v + "deg)"; },
    ty: function (v) { return "translateY(" + v + "px)"; },
    sx: function (v) { return "scaleX(" + v + ")"; },
    sk: function (v) { return "skewY(" + v + "deg)"; },
    xy: function (v) { return "translate(" + v[0] + "px," + v[1] + "px)"; },
    cup: function (v) { return "translateY(15px) rotate(" + v + "deg)"; },
    w: function (v) { return "translate(calc(var(--ck-wx) * " + v + "),calc(var(--ck-wy) * " + v + "))"; }
  };
  function kf(name, fn, frames) {
    frames = frames.slice().sort(function (a, b) { return a[0] - b[0]; });
    var s = "@keyframes " + name + "{";
    frames.forEach(function (f) {
      s += (+f[0].toFixed(2)) + "%{transform:" + FN[fn](f[1]) + (f[2] ? ";animation-timing-function:" + f[2] : "") + "}";
    });
    return s + "}";
  }
  // zig-zag between a% and b% (walking legs / arm swing), 0 at both ends
  function steps(a, b, n, amp) {
    var f = [];
    for (var i = 0; i <= n; i++) f.push([a + (b - a) * i / n, i === 0 || i === n ? 0 : (i % 2 ? amp : -amp)]);
    return f;
  }
  function neg(fr) { return fr.map(function (f) { return [f[0], -f[1], f[2]]; }); }

  function sceneCss() {
    var k = [], r = [];
    var tap = [[0, 12], [28, 12], [31, 17], [34, 12], [50, 12], [53, 17], [56, 12]];
    // batting, attacking shot (contact at 88% = when the ball arrives)
    k.push(kf("ck-bat-l", "r", tap.concat([[74, 12, "ease-in-out"], [81, -110], [85, -118, "ease-in"], [88, 20, "ease-out"], [92, 148], [95, 148, "ease-in-out"], [100, 12]])));
    k.push(kf("ck-bat-r", "r", [[0, 40], [74, 40, "ease-in-out"], [81, -95], [85, -100, "ease-in"], [88, 34, "ease-out"], [92, 120], [95, 120, "ease-in-out"], [100, 40]]));
    k.push(kf("ck-bat-b", "r", [[0, 0], [82, 0], [85, 2], [88, -4], [92, -6], [95, -6], [100, 0]]));
    k.push(kf("ck-bat-ll", "r", [[0, 0], [82, 0], [86, 18], [95, 18], [100, 0]]));
    k.push(kf("ck-bat-lr", "r", [[0, 0], [84, 0], [88, -6], [95, -6], [100, 0]]));
    // batting, forward defensive (the reviewed agent facing the bowling)
    k.push(kf("ck-def-l", "r", tap.concat([[74, 12, "ease-in-out"], [81, -46], [85, -50, "ease-in"], [88, 4, "ease-out"], [95, 4, "ease-in-out"], [100, 12]])));
    k.push(kf("ck-def-r", "r", [[0, 40], [74, 40], [81, -10], [85, -14, "ease-in"], [88, 30], [95, 30], [100, 40]]));
    k.push(kf("ck-def-b", "r", [[0, 0], [84, 0], [88, -7], [95, -7], [100, 0]]));
    k.push(kf("ck-def-ll", "r", [[0, 0], [82, 0], [87, 24], [96, 24], [100, 0]]));
    // non-striker: leans on the bat, backs up as the bowler runs in, walks back after the shot
    k.push(kf("ck-ns-run", "xy", [[0, [0, 0]], [58, [0, 0], "ease-in-out"], [74, [26, 0]], [88, [26, 0], "ease-in-out"], [100, [0, 0]]]));
    k.push(kf("ck-ns-b", "r", [[0, -6], [56, -6], [60, 0], [96, 0], [100, -6]]));
    k.push(kf("ck-ns-ll", "r", [[0, 0]].concat(steps(58, 74, 4, 18), steps(88, 100, 3, 14))));
    k.push(kf("ck-ns-lr", "r", [[0, 0]].concat(neg(steps(58, 74, 4, 18)), neg(steps(88, 100, 3, 14)))));
    k.push(kf("ck-ns-l", "r", [[0, 18], [56, 18], [60, 28], [90, 28], [96, 18], [100, 18]]));
    k.push(kf("ck-ns-r", "r", [[0, -28], [56, -28], [60, 0], [64, 16], [68, -16], [72, 16], [74, 0], [88, 0], [92, -12], [96, 8], [100, -28]]));
    // bowler: follow-through, walk back to the mark, run in, leap, arm over (release at 80%)
    k.push(kf("ck-bw-run", "xy", [[0, [RUN + 14, 0], "ease-out"], [6, [RUN + 22, 0]], [10, [RUN + 22, 0], "linear"], [40, [0, 0]],
      [44, [0, 0], "ease-in"], [74, [RUN - 28, 0], "linear"], [77, [RUN - 14, -7], "linear"], [80, [RUN, 0], "ease-out"], [100, [RUN + 14, 0]]]));
    k.push(kf("ck-bw-face", "sx", [[0, 1, "step-end"], [8, -1, "step-end"], [42, 1, "step-end"], [100, 1]]));
    k.push(kf("ck-bw-ll", "r", [[0, 0], [6, 0]].concat(steps(10, 40, 6, 20), steps(44, 74, 10, 28),
      [[77, 26], [80, 16], [84, -12], [88, 10], [92, -6], [96, 0], [100, 0]])));
    k.push(kf("ck-bw-lr", "r", [[0, 0], [6, 0]].concat(neg(steps(10, 40, 6, 20)), neg(steps(44, 74, 10, 28)),
      [[77, -28], [80, -18], [84, 12], [88, -10], [92, 6], [96, 0], [100, 0]])));
    k.push(kf("ck-bw-r", "r", [[0, 0], [10, 0]].concat(steps(10, 40, 6, 14), steps(44, 72, 8, 30),
      [[74, 30, "linear"], [77, 150, "linear"], [80, 215, "linear"], [82, 290, "linear"], [85, 350, "ease-out"], [88, 360], [100, 360]])));
    k.push(kf("ck-bw-l", "r", [[0, 0], [10, 0]].concat(neg(steps(10, 40, 6, 14)), neg(steps(44, 72, 8, 30)),
      [[74, -30], [77, -150], [80, -90], [84, 10], [90, 0], [100, 0]])));
    // fielders: walk in with the bowler, crouch for the delivery, walk back
    k.push(kf("ck-fd-run", "w", [[0, 0.667], [20, 0], [44, 0, "ease-in-out"], [76, 1], [90, 1, "ease-in-out"], [100, 0.667]]));
    k.push(kf("ck-fd-ll", "r", steps(0, 20, 3, 16).concat(steps(44, 76, 6, 18), steps(90, 100, 2, 16))));
    k.push(kf("ck-fd-lr", "r", neg(steps(0, 20, 3, 16)).concat(neg(steps(44, 76, 6, 18)), neg(steps(90, 100, 2, 16)))));
    k.push(kf("ck-fd-b", "ty", [[0, 0], [76, 0], [79, 5], [89, 5], [92, 0], [100, 0]]));
    k.push(kf("ck-fd-l", "r", [[0, 0], [76, 0], [79, -24], [89, -24], [92, 0], [100, 0]]));
    k.push(kf("ck-fd-r", "r", [[0, 0], [76, 0], [79, 24], [89, 24], [92, 0], [100, 0]]));
    // dugout routines
    var sip = [[0, -6], [52, -6, "ease-in-out"], [60, 125], [74, 125, "ease-in-out"], [82, -6], [100, -6]];
    k.push(kf("ck-sip-r", "r", sip));
    k.push(kf("ck-sip-c", "cup", sip));
    k.push(kf("ck-sip-h", "r", [[0, 0], [60, 0], [64, -6], [72, -6], [78, 0], [100, 0]]));
    k.push(kf("ck-chat-h", "r", [[0, 0], [14, 0], [20, 9], [52, 9], [58, 0], [100, 0]]));
    k.push(kf("ck-chat-l", "r", [[0, 6], [22, 6], [26, -28], [30, -14], [34, -32], [38, -10], [44, 6], [100, 6]]));
    var clap = [[0, 6], [29.4, 6], [30.2, 46], [31.2, 28], [32.2, 46], [33.2, 28], [34.2, 46], [35.4, 6], [100, 6]];
    k.push(kf("ck-clap-l", "r", neg(clap)));
    k.push(kf("ck-clap-r", "r", clap));
    // blocked flag, celebration
    k.push(kf("ck-flag-r", "r", [[0, 166], [100, 194]]));
    k.push(kf("ck-flutter", "sk", [[0, -9], [100, 9]]));
    k.push(kf("ck-hop", "xy", [[0, [0, 0], "ease-out"], [45, [0, -9], "ease-in"], [90, [0, 0]], [100, [0, 0]]]));
    k.push(kf("ck-wave-l", "r", [[0, -166], [50, -188], [100, -166]]));
    k.push(kf("ck-wave-r", "r", [[0, 166], [50, 188], [100, 166]]));
    // ball: x linear, y eased (gravity), opacity gate; shadow shares x
    var R = G.rel, B = G.bounce, H = G.hit;
    k.push("@keyframes ck-bx{0%,80%{transform:translateX(" + R[0] + "px);animation-timing-function:linear}85.5%{transform:translateX(" + B[0] +
      "px);animation-timing-function:linear}88%{transform:translateX(" + H[0] + "px);animation-timing-function:linear}94%{transform:translateX(var(--ck-mx));animation-timing-function:linear}100%{transform:translateX(var(--ck-ex))}}");
    k.push("@keyframes ck-by{0%,80%{transform:translateY(" + R[1] + "px);animation-timing-function:ease-in}85.5%{transform:translateY(" + B[1] +
      "px);animation-timing-function:ease-out}88%{transform:translateY(" + H[1] + "px);animation-timing-function:ease-out}94%{transform:translateY(var(--ck-my));animation-timing-function:ease-in}100%{transform:translateY(var(--ck-ey))}}");
    k.push("@keyframes ck-sy{0%,80%{transform:translateY(372px)}85.5%,88%{transform:translateY(371px)}94%{transform:translateY(var(--ck-sm))}100%{transform:translateY(var(--ck-se))}}");
    k.push("@keyframes ck-bo{0%,79.9%{opacity:0}80%,96%{opacity:1}100%{opacity:0}}");
    k.push("@keyframes ck-fall{0%{transform:translate(0,-250px) rotate(0);opacity:0}8%{opacity:1}85%{opacity:1}100%{transform:translate(var(--dx),24px) rotate(540deg);opacity:0}}");
    k.push("@keyframes ck-burst{0%{transform:scale(.15);opacity:0}12%{opacity:1}70%{transform:scale(1);opacity:.9}100%{transform:scale(1.12);opacity:0}}");
    k.push("@keyframes ck-roll{from{transform:translateY(9px);opacity:0}to{transform:none;opacity:1}}");
    k.push(kf("ck-polish", "r", [[0, -4], [100, -20]]));
    k.push("@keyframes ck-pflag{from{transform:skewY(-6deg) scaleX(.94)}to{transform:skewY(5deg) scaleX(1)}}");

    // rules: base transform = the composed still pose (what reduced-motion users see)
    function rule(sel, name, dur, base, extra) {
      var s = sel.split(",").map(function (x) { return ".ck-a." + x.trim(); }).join(",");
      r.push(s + "{transform:" + base + ";animation:" + name + " " + dur + "s ease-in-out infinite" + (extra || "") +
             ";animation-delay:calc(var(--ck-d,0s) + var(--ck-o,0s))}");
    }
    rule("ck-bat .p-arm-l,ck-bat .ck-p-bat", "ck-bat-l", P, "rotate(12deg)");
    rule("ck-bat .p-arm-r", "ck-bat-r", P, "rotate(40deg)");
    rule("ck-bat .p-body,ck-bat .ck-onbody", "ck-bat-b", P, "none");
    rule("ck-bat .p-leg-l", "ck-bat-ll", P, "none");
    rule("ck-bat .p-leg-r", "ck-bat-lr", P, "none");
    rule("ck-def .p-arm-l,ck-def .ck-p-bat", "ck-def-l", P, "rotate(12deg)");
    rule("ck-def .p-arm-r", "ck-def-r", P, "rotate(40deg)");
    rule("ck-def .p-body,ck-def .ck-onbody", "ck-def-b", P, "none");
    rule("ck-def .p-leg-l", "ck-def-ll", P, "none");
    rule("ck-ns .ck-run", "ck-ns-run", P, "none");
    rule("ck-ns .p-body,ck-ns .ck-onbody", "ck-ns-b", P, "rotate(-6deg)");
    rule("ck-ns .p-leg-l", "ck-ns-ll", P, "none");
    rule("ck-ns .p-leg-r", "ck-ns-lr", P, "none");
    rule("ck-ns .p-arm-l,ck-ns .ck-p-bat", "ck-ns-l", P, "rotate(18deg)");
    rule("ck-ns .p-arm-r", "ck-ns-r", P, "rotate(-28deg)");
    rule("ck-bowl .ck-run", "ck-bw-run", P, "translate(" + RUN + "px,0)");
    rule("ck-bowl .ck-face", "ck-bw-face", P, "none");
    rule("ck-bowl .p-leg-l", "ck-bw-ll", P, "rotate(16deg)");
    rule("ck-bowl .p-leg-r", "ck-bw-lr", P, "rotate(-18deg)");
    rule("ck-bowl .p-arm-r", "ck-bw-r", P, "rotate(200deg)");
    rule("ck-bowl .p-arm-l", "ck-bw-l", P, "rotate(-120deg)");
    rule("ck-field .ck-run", "ck-fd-run", P, "none");
    rule("ck-field .p-leg-l", "ck-fd-ll", P, "none");
    rule("ck-field .p-leg-r", "ck-fd-lr", P, "none");
    rule("ck-field .p-body,ck-field .ck-onbody", "ck-fd-b", P, "translateY(4px)");
    rule("ck-field .p-arm-l", "ck-fd-l", P, "rotate(-20deg)");
    rule("ck-field .p-arm-r", "ck-fd-r", P, "rotate(20deg)");
    rule("ck-sit.ck-sip .p-arm-r", "ck-sip-r", 7, "rotate(-6deg)");
    rule("ck-sit.ck-sip .ck-p-cup", "ck-sip-c", 7, "translateY(15px) rotate(-6deg)");
    rule("ck-sit.ck-sip .p-head", "ck-sip-h", 7, "none");
    rule("ck-sit.ck-chat .p-head", "ck-chat-h", 6, "none");
    rule("ck-sit.ck-chat .p-arm-l", "ck-chat-l", 6, "rotate(6deg)");
    rule("ck-sit.ck-clap .p-arm-l", "ck-clap-l", 3 * P, "rotate(6deg)");
    rule("ck-sit.ck-clap .p-arm-r", "ck-clap-r", 3 * P, "rotate(-6deg)");
    rule("ck-block .p-arm-r,ck-block .ck-p-flag", "ck-flag-r", 1.8, "rotate(178deg)", " alternate");
    rule("ck-block .ck-flag-cloth", "ck-flutter", 0.7, "none", " alternate");
    rule("ck-cheer .ck-run", "ck-hop", 0.9, "none");
    rule("ck-ready .p-arm-r", "ck-polish", 0.45, "rotate(-8deg)", " alternate");
    rule("ck-cheer .p-arm-l,ck-cheer .ck-p-bat,ck-cheer .ck-p-trophy", "ck-wave-l", 1.8, "rotate(-176deg)");
    rule("ck-cheer .p-arm-r", "ck-wave-r", 1.8, "rotate(176deg)");

    var c = [
      ".ck-scene text{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}",
      ".ck-prop{display:none}",
      ".ck-has-bat .ck-p-bat,.ck-has-helmet .ck-p-helmet,.ck-has-cup .ck-p-cup,.ck-has-flag .ck-p-flag,.ck-has-trophy .ck-p-trophy,.ck-has-board .ck-board,.ck-captain .ck-p-badge{display:inline}",
      ".ck-padded .p-leg-l,.ck-padded .p-leg-r{fill:#f5f3ec;stroke:#cbc3b0;stroke-width:1}",
      ".ck-p-bat,.ck-p-trophy{transform-box:view-box;transform-origin:-15.5px -60px}",
      ".ck-p-cup,.ck-p-flag{transform-box:view-box;transform-origin:15.5px -60px}",
      ".ck-onbody,.ck-run,.ck-face{transform-box:view-box;transform-origin:0 0}",
      ".ck-flag-cloth{transform-box:fill-box;transform-origin:0 50%}",
      // sitting: hips drop onto the bench, thighs foreshortened towards the camera
      ".ck-a.ck-sit .p-body,.ck-a.ck-sit .ck-onbody{transform:translateY(15px)}",
      ".ck-a.ck-sit .p-leg-l,.ck-a.ck-sit .p-leg-r{transform:scaleY(.5)}",
      ".ck-a.ck-sit .p-arm-l{transform:rotate(6deg)}.ck-a.ck-sit .p-arm-r{transform:rotate(-6deg)}",
      ".ck-a.ck-sit .ck-p-cup{transform:translateY(15px) rotate(-6deg)}",
      ".ck-a.ck-seated .ck-tagw{transform:translateY(15px)}.ck-a.ck-seated.ck-alt .ck-tagw{transform:translateY(-9px)}",
      ".ck-a.ck-block .p-arm-l{transform:rotate(-10deg)}.ck-a.ck-block .ck-tagw{transform:translate(-32px,4px)}",
      ".ck-a.ck-cheer .ck-tagw{transform:translateY(-24px)}.ck-a.ck-cheer.ck-alt .ck-tagw{transform:translateY(-46px)}",
      // props follow the shared walking cycle (same keyframes + timing as the arms in office.css)
      ".ck-a .person.is-walking .ck-p-bat,.ck-a .person.is-walking .ck-p-trophy{animation:oo-swing .6s ease-in-out infinite alternate-reverse}",
      ".ck-a .person.is-walking .ck-p-cup,.ck-a .person.is-walking .ck-p-flag{animation:oo-swing .6s ease-in-out infinite alternate}",
      ".ck-a .person.is-walking .ck-onbody{animation:oo-bob .3s ease-in-out infinite alternate}",
      ".ck-a.ck-runner .person.is-walking .p-limb,.ck-a.ck-runner .person.is-walking .ck-p-bat{animation-duration:.3s}",
      ".ck-a.ck-runner .person.is-walking .p-body,.ck-a.ck-runner .person.is-walking .ck-onbody{animation-duration:.15s}",
      ".ck-chip rect{fill:#fffdf8;stroke:var(--c);stroke-width:1.4}",
      ".ck-chip text{font-size:10.5px;font-weight:700;fill:var(--c)}",
      ".ck-board text{font-weight:700}",
      // ball
      ".ck-ball{display:none}.ck-ball.ck-on{display:inline}",
      ".ck-bx,.ck-by,.ck-bo,.ck-sy{transform-box:view-box;transform-origin:0 0}",
      ".ck-on .ck-bx{animation:ck-bx " + P + "s linear infinite}.ck-on .ck-by{animation:ck-by " + P + "s linear infinite}",
      ".ck-on .ck-sy{animation:ck-sy " + P + "s linear infinite}.ck-on .ck-bo{animation:ck-bo " + P + "s linear infinite}",
      ".ck-on .ck-bx,.ck-on .ck-by,.ck-on .ck-sy,.ck-on .ck-bo{animation-delay:var(--ck-d,0s)}",
      // scoreboard + celebration
      ".ck-sb .ck-roll{animation:ck-roll .5s cubic-bezier(.2,.8,.2,1) both}",
      ".ck-sb .ck-won{display:none}.ck-sb.ck-is-won .ck-won{display:inline}.ck-sb.ck-is-won .ck-need{display:none}",
      ".ck-conf rect{transform-box:fill-box;transform-origin:center;animation:ck-fall var(--dur) cubic-bezier(.3,.6,.5,1) var(--del) 2 both}",
      ".ck-burst{transform-box:view-box;transform-origin:0 0;animation:ck-burst 1.6s cubic-bezier(.2,.8,.2,1) var(--del) 3 both}",
      ".ck-pflag{transform-box:fill-box;transform-origin:0 50%;animation:ck-pflag 2.6s ease-in-out infinite alternate}"
    ];
    return k.join("\n") + "\n" + r.join("\n") + "\n" + c.join("\n");
  }

  // ------------------------------------------------------------------ the ground (drawn once)
  function drawGround(svg, S) {
    var i, defs = S("defs", {}, svg);
    var sky = S("linearGradient", { id: "ck-sky", x1: 0, y1: 0, x2: 0, y2: 1 }, defs);
    S("stop", { offset: 0, "stop-color": "#c4dce4" }, sky);
    S("stop", { offset: 1, "stop-color": "#eef0e2" }, sky);
    var glow = S("radialGradient", { id: "ck-glow", cx: 0.52, cy: 0.6, r: 0.55 }, defs);
    S("stop", { offset: 0, "stop-color": "#fff8dc", "stop-opacity": 0.22 }, glow);
    S("stop", { offset: 1, "stop-color": "#fff8dc", "stop-opacity": 0 }, glow);
    var clip = S("clipPath", { id: "ck-grass" }, defs);
    S("rect", { x: 0, y: 186, width: 1000, height: 374 }, clip);

    S("rect", { x: 0, y: 0, width: 1000, height: 200, fill: "url(#ck-sky)" }, svg);

    // stands: roof, tiers, a seeded crowd
    var stn = S("g", { "aria-hidden": "true" }, svg);
    S("rect", { x: 0, y: 40, width: 1000, height: 146, fill: "#5b676d" }, stn);
    for (i = 0; i < 5; i++) S("rect", { x: 0, y: 60 + i * 25, width: 1000, height: 3, fill: "#4c575c" }, stn);
    S("path", { d: "M0,26 H1000 V40 L0,46Z", fill: "#e6e0d1" }, stn);
    S("path", { d: "M0,46 L1000,40 V44 L0,50Z", fill: "#00000022" }, stn);
    var rand = rng(7), crowd = ["#e9d8b4", "#c96f53", "#3f6e8c", "#e3b448", "#9aa8b1", "#f1efe8", "#6d8f5a", "#b5556b", "#d98b4a"];
    var skin = ["#e9b997", "#c58b5c", "#f1c8a3", "#8b5a3c", "#d9a67e"];
    for (var row = 0; row < 5; row++) {
      var y = 72 + row * 25;
      for (var x = 318 + (row % 2) * 4; x < 1000; x += 9 + rand() * 3) {
        if (x > 766 && y < 150) continue;          // behind the scoreboard
        if (rand() < 0.12) continue;               // empty seats
        S("rect", { x: x - 3, y: y - 1, width: 6.4, height: 8, rx: 2.4, fill: crowd[Math.floor(rand() * crowd.length)] }, stn);
        S("circle", { cx: x + 0.2, cy: y - 4, r: 2.7, fill: skin[Math.floor(rand() * skin.length)] }, stn);
      }
    }
    // perimeter wall with boards
    S("rect", { x: 0, y: 174, width: 1000, height: 14, fill: "#2c5644" }, stn);
    for (i = 0; i < 8; i++) {
      var bx = 330 + i * 84;
      if (bx > 990) break;
      S("rect", { x: bx, y: 176, width: 78, height: 10, rx: 1.5, fill: i % 2 ? "#f2ead6" : "#24493a" }, stn);
      var bt = S("text", { x: bx + 39, y: 184, "text-anchor": "middle", "font-size": 7.5, "font-weight": 800,
        "letter-spacing": 1.5, fill: i % 2 ? "#2c5644" : "#f2ead6" }, stn);
      bt.textContent = i % 2 ? "omnicli" : "team run";
    }

    // the field: mowing stripes, light, boundary rope, inner circle
    var fld = S("g", { "aria-hidden": "true" }, svg);
    S("rect", { x: 0, y: 186, width: 1000, height: 374, fill: "#68a454" }, fld);
    var stripes = S("g", { "clip-path": "url(#ck-grass)" }, fld);
    for (i = 0; i < 16; i += 2) S("path", { d: "M" + (i * 64 - 40) + ",186 h64 l40,374 h-64z", fill: "#72ad5c" }, stripes);
    S("rect", { x: 0, y: 186, width: 1000, height: 374, fill: "url(#ck-glow)" }, fld);
    S("ellipse", { cx: 500, cy: 702, rx: 900, ry: 520, fill: "none", stroke: "#00000018", "stroke-width": 3, "clip-path": "url(#ck-grass)" }, fld);
    S("ellipse", { cx: 500, cy: 700, rx: 900, ry: 520, fill: "none", stroke: "#f8f5ea", "stroke-width": 2.6, "clip-path": "url(#ck-grass)" }, fld);
    S("ellipse", { cx: 510, cy: 362, rx: 300, ry: 128, fill: "none", stroke: "#ffffff", "stroke-opacity": 0.45,
      "stroke-width": 1.6, "stroke-dasharray": "2 8", "stroke-linecap": "round" }, fld);

    // the pitch, creases and both sets of stumps
    S("path", { d: "M298,346 H722 L728,375 H292Z", fill: "#dcc794" }, fld);
    S("ellipse", { cx: 350, cy: 361, rx: 40, ry: 10, fill: "#cdb47b", opacity: 0.7 }, fld);
    S("ellipse", { cx: 670, cy: 361, rx: 40, ry: 10, fill: "#cdb47b", opacity: 0.7 }, fld);
    var crease = { stroke: "#fbf7ea", "stroke-width": 1.8, "stroke-linecap": "round", fill: "none" };
    [[345, 365, 1], [675, 655, -1]].forEach(function (c) {
      S("path", Object.assign({ d: "M" + c[0] + ",341 L" + c[0] + ",380 M" + c[1] + ",334 L" + c[1] + ",388" +
        " M" + c[0] + ",344 H" + (c[1] + c[2] * 14) + " M" + c[0] + ",377 H" + (c[1] + c[2] * 14) }, crease), fld);
    });
    [345, 675].forEach(function (sx) {
      S("ellipse", { cx: sx + 3, cy: 360, rx: 9, ry: 2.4, fill: "#00000022" }, fld);
      for (var s = -1; s <= 1; s++) S("rect", { x: sx + s * 4 - 1.1, y: 332, width: 2.2, height: 28, rx: 0.8, fill: "#f3ead2", stroke: "#b9a77d", "stroke-width": 0.5 }, fld);
      S("rect", { x: sx - 5.5, y: 330.5, width: 5.2, height: 1.8, rx: 0.8, fill: "#e8dcbc" }, fld);
      S("rect", { x: sx + 0.3, y: 330.5, width: 5.2, height: 1.8, rx: 0.8, fill: "#e8dcbc" }, fld);
    });
  }

  function drawPavilion(svg, S) {
    var i, pv = S("g", { "aria-hidden": "true" }, svg);
    S("rect", { x: 22, y: 40, width: 284, height: 150, fill: "#efe5cf" }, pv);
    S("path", { d: "M12,46 L164,8 L316,46Z", fill: "#2f5a47" }, pv);
    S("rect", { x: 12, y: 44, width: 304, height: 6, fill: "#24493a" }, pv);
    S("circle", { cx: 164, cy: 31, r: 9, fill: "#fffaf0", stroke: "#c9b98f", "stroke-width": 1.5 }, pv);
    S("path", { d: "M164,31 v-6 M164,31 h4.5", stroke: "#2f3a40", "stroke-width": 1.4, "stroke-linecap": "round" }, pv);
    S("rect", { x: 163, y: -6, width: 1.6, height: 16, fill: "#6b6152" }, pv);
    S("path", { "class": "ck-pflag", d: "M164.6,-5 h17 l-3,5 l3,5 h-17z", fill: "#d4614f" }, pv);
    for (i = 0; i < 7; i++) S("path", { d: "M" + (38 + i * 38) + ",84 v-16 a8,8 0 0 1 16,0 v16z", fill: "#3f525b" }, pv);
    S("rect", { x: 22, y: 94, width: 284, height: 4, fill: "#fffaf0" }, pv);
    for (i = 0; i < 36; i++) S("rect", { x: 24 + i * 8, y: 98, width: 1.6, height: 10, fill: "#fffaf0" }, pv);
    // dugout: dark interior, canopy, bench
    S("rect", { x: 20, y: 112, width: 288, height: 84, fill: "#264536" }, pv);
    S("rect", { x: 20, y: 112, width: 288, height: 16, fill: "#00000030" }, pv);
    S("path", { d: "M12,106 H316 L320,119 H8Z", fill: "#f6f1e4" }, pv);
    S("rect", { x: 8, y: 118, width: 312, height: 3, fill: "#2f5a47" }, pv);
    [16, 312].forEach(function (px) { S("rect", { x: px - 2, y: 121, width: 4, height: 75, fill: "#e9e2d0" }, pv); });
    S("rect", { x: 26, y: 176, width: 276, height: 6, rx: 2, fill: "#9a7550" }, pv);
    S("rect", { x: 26, y: 182, width: 276, height: 3, fill: "#6e5236" }, pv);
    S("rect", { x: 10, y: 194, width: 308, height: 5, fill: "#d8cfba" }, pv);
  }

  function drawScoreboard(svg, S) {
    var sb = S("g", { "class": "ck-sb", transform: "translate(772,12)" }, svg);
    S("rect", { x: 96, y: 128, width: 20, height: 40, fill: "#3a4a44" }, sb);
    S("rect", { x: 0, y: 0, width: 212, height: 130, rx: 7, fill: "#1d3b2f", stroke: "#11261d", "stroke-width": 3 }, sb);
    var o = {};
    o.team = S("text", { x: 12, y: 22, "font-size": 12, "font-weight": 700, fill: "#f3ead2" }, sb);
    S("rect", { x: 10, y: 30, width: 192, height: 1, fill: "#ffffff24" }, sb);
    var sc = S("g", {}, sb);
    o.score = S("text", { x: 14, y: 80, "font-size": 44, "font-weight": 800, fill: "#f6c95b",
      style: "font-variant-numeric:tabular-nums" }, sc);
    o.scoreG = sc;
    o.labels = S("text", { x: 15, y: 96, "font-size": 9.5, fill: "#b9c7b8" }, sb);
    o.labels.textContent = "runs / wickets";
    o.target = S("text", { x: 200, y: 52, "text-anchor": "end", "font-size": 11.5, fill: "#d9d2bd" }, sb);
    o.need = S("text", { "class": "ck-need", x: 200, y: 78, "text-anchor": "end", "font-size": 17, "font-weight": 800, fill: "#f3ead2" }, sb);
    o.won = S("text", { "class": "ck-won", x: 200, y: 84, "text-anchor": "end", "font-size": 30, "font-weight": 900,
      fill: "#f6c95b", "letter-spacing": 2 }, sb);
    o.won.textContent = "WON";
    S("rect", { x: 6, y: 104, width: 200, height: 20, rx: 4, fill: "#12281f" }, sb);
    o.status = S("text", { x: 106, y: 118, "text-anchor": "middle", "font-size": 10.5, fill: "#e7dfc8" }, sb);
    o.root = sb;
    return o;
  }

  function drawUmpire(layer, api) {
    var u = api.svg("g", { transform: "translate(" + G.umpire[0] + "," + G.umpire[1] + ") scale(" + SC + ")", "aria-hidden": "true" }, layer);
    u.appendChild(api.person({ name: "Umpire", skin: "#c99a74", hair: "#d8d4cc", hairStyle: "short", shirt: "#f4f1e8",
                               pants: "#2b2f36", accessory: "none", number: 0 }));
    api.svg("ellipse", { cx: 0, cy: -85, rx: 19, ry: 4, fill: "#f7f4ec", stroke: "#d6d0c2", "stroke-width": 0.8 }, u);
    api.svg("path", { d: "M-11,-86 a11,9 0 0 1 22,0z", fill: "#f7f4ec", stroke: "#d6d0c2", "stroke-width": 0.8 }, u);
  }

  // ------------------------------------------------------------------ agents
  function buildPerson(e, c) {
    var S = st.api.svg;
    if (e.person) e.person.remove();
    var p = st.api.person(c);
    e.face.appendChild(p);
    e.person = p;
    e.charKey = JSON.stringify(c);
    var prop = p.querySelector(".p-prop");
    // bat hangs from the left shoulder (-15.5,-60): handle at the hand, blade to the ground
    var bat = S("g", { "class": "ck-prop ck-p-bat" }, prop);
    S("rect", { x: -16.8, y: -37, width: 2.6, height: 15, rx: 1.2, fill: "#3b2b20" }, bat);
    S("rect", { x: -19.4, y: -23, width: 7.8, height: 25, rx: 2.4, fill: "#ecd49c", stroke: "#b38d52", "stroke-width": 0.9 }, bat);
    S("rect", { x: -17.2, y: -19, width: 3.4, height: 6, rx: 1, fill: c.shirt, opacity: 0.85 }, bat);
    // drinks bottle in the right hand
    var cup = S("g", { "class": "ck-prop ck-p-cup" }, prop);
    S("rect", { x: 12.2, y: -37, width: 6.6, height: 10, rx: 1.6, fill: "#e9f1f4", stroke: "#9fb4bd", "stroke-width": 0.7 }, cup);
    S("rect", { x: 13.2, y: -39.5, width: 4.6, height: 3, rx: 1, fill: c.shirt }, cup);
    // red flag in the right hand (raised overhead by the pose)
    var flag = S("g", { "class": "ck-prop ck-p-flag" }, prop);
    S("rect", { x: 14.6, y: -37, width: 1.8, height: 52, fill: "#6b5a48" }, flag);
    S("path", { "class": "ck-flag-cloth", d: "M16.4,-1 h17 l-3,7 l3,7 h-17z", fill: "var(--blocked)" }, flag);
    // trophy for the captain: drawn upside down in the hanging-arm frame because it is held aloft
    var tr = S("g", { "class": "ck-prop ck-p-trophy" }, prop);
    S("rect", { x: -19, y: -37, width: 7, height: 3.5, rx: 1, fill: "#a87a22" }, tr);
    S("rect", { x: -16.6, y: -34, width: 2.2, height: 7, fill: "#d9a93a" }, tr);
    S("path", { d: "M-23.5,-12 V-19 A8,9 0 0 1 -7.5,-19 V-12 Z", fill: "#e8b84a", stroke: "#a87a22", "stroke-width": 0.8 }, tr);
    S("path", { d: "M-23.5,-17 q-5,2 -1,7 M-7.5,-17 q5,2 1,7", fill: "none", stroke: "#e8b84a", "stroke-width": 1.6 }, tr);
    // things worn on the body follow the body's transforms (.ck-onbody mirrors .p-body)
    var ob = S("g", { "class": "ck-onbody" }, prop);
    var hc = shade(c.shirt, -0.22), hm = S("g", { "class": "ck-prop ck-p-helmet" }, ob);
    S("path", { d: "M-13.6,-78.5 a13.6,14 0 0 1 27.2,0z", fill: hc }, hm);
    S("path", { d: "M-6,-90 q6,-3 12,0", fill: "none", stroke: "#ffffff55", "stroke-width": 1.4, "stroke-linecap": "round" }, hm);
    S("rect", { x: -15, y: -81.5, width: 30, height: 3, rx: 1.4, fill: shade(hc, -0.3) }, hm);
    S("path", { d: "M-8,-74.5 v8 M-3,-74.5 v9 M3,-74.5 v9 M8,-74.5 v8 M-9,-70.5 h18 M-8,-66.5 h16", stroke: "#cfd6da",
      "stroke-width": 1.1, fill: "none", "stroke-linecap": "round" }, hm);
    var bd = S("g", { "class": "ck-prop ck-p-badge" }, ob);
    S("circle", { cx: -6.5, cy: -53, r: 4.6, fill: "#f2c14e", stroke: "#8a6a1a", "stroke-width": 0.8 }, bd);
    var t = S("text", { x: -6.5, y: -50.6, "text-anchor": "middle", "font-size": 6.8, "font-weight": 800, fill: "#4a3410" }, bd);
    t.textContent = "C";
  }

  function makeEntry(a) {
    var S = st.api.svg, e = { id: a.id, role: null, key: null, tok: 0, timer: 0, pose: null, tagText: null, chipText: null };
    e.root = S("g", { "class": "ck-a", "data-id": a.id }, st.people);
    e.run = S("g", { "class": "ck-run" }, e.root);
    var sc = S("g", { transform: "scale(" + SC + ")" }, e.run);
    e.face = S("g", { "class": "ck-face" }, sc);
    var extra = S("g", {}, sc);                         // never mirrored: tag, chip, board
    e.tagw = S("g", { "class": "ck-tagw" }, extra);
    e.chip = S("g", { "class": "ck-chip", transform: "translate(0,20)" }, extra);
    e.chipRect = S("rect", { x: -20, y: -9, width: 40, height: 17, rx: 8.5 }, e.chip);
    e.chipLabel = S("text", { x: 0, y: 3.6, "text-anchor": "middle" }, e.chip);
    e.board = S("g", { "class": "ck-prop ck-board", transform: "translate(28,0)" }, extra);
    S("rect", { x: 30, y: -42, width: 3, height: 42, fill: "#6b5a48" }, e.board);
    S("rect", { x: -2, y: -82, width: 92, height: 40, rx: 5, fill: "#fffaf0", stroke: "var(--blocked)", "stroke-width": 2 }, e.board);
    e.boardT1 = S("text", { x: 44, y: -66, "text-anchor": "middle", "font-size": 12, fill: "var(--blocked)" }, e.board);
    e.boardT2 = S("text", { x: 44, y: -51, "text-anchor": "middle", "font-size": 9.5, fill: "#4f5d70" }, e.board);
    buildPerson(e, a.character);
    return e;
  }

  function setTag(e, text) {
    if (e.tagText === text) return;
    e.tagText = text;
    e.tagw.textContent = "";
    e.tagw.appendChild(st.api.tag(text));
  }
  function setChip(e, text, color) {
    e.chip.style.display = text ? "" : "none";
    e.chip.style.setProperty("--c", color || "var(--free)");
    if (!text || e.chipText === text) return;
    e.chipText = text;
    e.chipLabel.textContent = text;
    var w = Math.max(30, text.length * 6.1 + 16);
    e.chipRect.setAttribute("x", -w / 2);
    e.chipRect.setAttribute("width", w);
  }

  // keep painter's order by ground position (only the arriving node moves, so nobody else restarts)
  function depth(e) {
    var y = +e.root.dataset.y, kids = st.people.children;
    for (var i = 0; i < kids.length; i++) {
      var k = kids[i];
      if (k === e.root) continue;
      if (+k.dataset.y > y) { if (k !== e.root.nextSibling) st.people.insertBefore(e.root, k); return; }
    }
    if (st.people.lastChild !== e.root) st.people.appendChild(e.root);
  }

  // stop the pose loop; fold any animated offset (run-up, backing up) into the position so nothing jumps
  function stopPose(e) {
    clearTimeout(e.timer);
    if (!e.pose) return;
    var dx = 0, dy = 0;
    try {
      var m = new DOMMatrixReadOnly(getComputedStyle(e.run).transform);
      dx = m.e; dy = m.f;
    } catch (err) { /* "none" or unsupported: no offset */ }
    ALL_POSE.forEach(function (c) { e.root.classList.remove(c); });
    e.pose = null;
    if (Math.abs(dx) > 0.5 || Math.abs(dy) > 0.5) {
      st.api.place(e.root, +e.root.dataset.x + dx, +e.root.dataset.y + dy);
      void e.root.getBoundingClientRect();   // commit the placed position before a walk starts from it
    }
  }

  function applyPose(e, asg) {
    var cls = POSE_CLS[asg.role].slice();
    e.root.classList.remove("ck-ready");
    if (asg.role === "sit") { cls.push("ck-seated", ROUTINES[asg.seat % ROUTINES.length]); if (asg.seat % 2) cls.push("ck-alt"); }
    if (asg.alt) cls.push("ck-alt");
    e.root.style.setProperty("--ck-d", clockDelay());
    e.root.style.setProperty("--ck-o", (asg.offset || 0) + "s");
    cls.forEach(function (c) { e.root.classList.add(c); });
    e.pose = cls;
    refreshBall();
  }

  function startPose(e, asg) {
    var win = WINDOW[asg.role], tok = e.tok;
    if (!win || st.api.reducedMotion) { applyPose(e, asg); return; }
    var ph = phase();
    if (ph >= win[0] && ph <= win[1]) { applyPose(e, asg); return; }
    if (asg.role === "bowl") e.root.classList.add("ck-ready");   // polish the ball at the mark
    e.timer = setTimeout(function () { if (st && e.tok === tok) applyPose(e, asg); },
                         (((win[0] - ph) % 1 + 1) % 1) * P * 1000 + 40);
  }

  function setProps(e, asg, a) {
    var role = asg.role, cl = e.root.classList, batter = /^(strike|defend|nsbat|pad)$/.test(role);
    cl.toggle("ck-has-bat", batter || role === "ns" || (role === "cheer" && a.role !== "lead"));
    cl.toggle("ck-has-helmet", batter);
    cl.toggle("ck-has-cup", role === "sit");
    cl.toggle("ck-has-flag", role === "block");
    cl.toggle("ck-has-board", role === "block");
    cl.toggle("ck-has-trophy", role === "cheer" && a.role === "lead");
    cl.toggle("ck-padded", batter || /^(ns|sit|cheer)$/.test(role));
    if (asg.walk) {
      e.root.style.setProperty("--ck-wx", asg.walk[0] + "px");
      e.root.style.setProperty("--ck-wy", asg.walk[1] + "px");
    }
  }

  function setRole(e, asg, a, first) {
    var key = asg.role + "@" + asg.x + "," + asg.y + "#" + (asg.seat || 0);
    if (e.key === key) return;
    var prev = e.role;
    e.key = key; e.role = asg.role; e.tok++;
    var tok = e.tok;
    stopPose(e);
    setProps(e, asg, a);
    if (first) { st.api.place(e.root, asg.x, asg.y); depth(e); startPose(e, asg); return; }
    var crossing = (prev === "strike" && asg.role === "nsbat") || (prev === "nsbat" && asg.role === "strike");
    e.root.classList.toggle("ck-runner", crossing);
    st.api.moveTo(e.root, asg.x, asg.y, { walk: true }).then(function () {
      if (!st || e.tok !== tok) return;
      e.root.classList.remove("ck-runner");
      depth(e);
      startPose(e, asg);
    });
  }

  // ------------------------------------------------------------------ who goes where
  function walkTo(p, len) {
    var dx = G.strike[0] - p[0], dy = G.strike[1] - p[1], d = Math.hypot(dx, dy) || 1;
    return [+(dx / d * len).toFixed(1), +(dy / d * len).toFixed(1)];
  }
  function spot(list, i, dx) { var b = list[i % list.length]; return [b[0] + Math.floor(i / list.length) * dx, b[1]]; }

  function assign(model) {
    var out = {}, ags = model.agents, idx = {};
    ags.forEach(function (a, i) { idx[a.id] = i; });
    function put(a, role, pos, extra) {
      if (!a) return;
      var o = { role: role, x: pos[0], y: pos[1] };
      Object.keys(extra || {}).forEach(function (k) { o[k] = extra[k]; });
      out[a.id] = o;
    }
    if (model.run.complete) {   // line up by the pavilion, captain in the middle
      var leads = ags.filter(function (a) { return a.role === "lead"; });
      var order = ags.filter(function (a) { return a.role !== "lead"; });
      Array.prototype.splice.apply(order, [Math.floor(order.length / 2), 0].concat(leads));
      order.forEach(function (a, i) { put(a, "cheer", spot(G.cheer, i, 35), { offset: -(i * 0.17).toFixed(2), alt: i % 2 }); });
      return { map: out, pair: null, targets: {} };
    }
    function by(p) { return ags.filter(function (a) { return a.pose === p; }); }
    var working = by("working"), reviewers = by("reviewing"), waiting = by("waiting");
    var isWorking = {}, targets = {};
    working.forEach(function (a) { isWorking[a.id] = true; });
    reviewers.forEach(function (r) { if (r.target) targets[r.target] = r; });
    // the bowler is a reviewer whose target is batting, else the first reviewer
    var bowler = reviewers.filter(function (r) { return r.target && isWorking[r.target]; })[0] || reviewers[0] || null;
    var pair = working.slice(0, 2), striker = null, nsBat = null, lock = false;
    var pairKey = pair.map(function (a) { return a.id; }).join("|");
    if (pairKey !== st.pairKey) { st.pairKey = pairKey; st.swapped = false; }
    if (pair.length) {
      var tgt = bowler && bowler.target ? pair.filter(function (p) { return p.id === bowler.target; })[0] : null;
      striker = tgt || pair[st.swapped && pair.length === 2 ? 1 : 0];
      lock = !!tgt;   // the reviewed batter stays on strike
      nsBat = pair.length === 2 ? (pair[0] === striker ? pair[1] : pair[0]) : null;
    }
    // nobody batting: the agent under review takes strike and defends
    var defender = null;
    if (!striker && bowler && bowler.target) defender = waiting.filter(function (w) { return w.id === bowler.target; })[0] || null;
    var rest = waiting.filter(function (w) { return w !== defender; }).sort(function (a, b) {
      return ((targets[b.id] ? 1 : 0) - (targets[a.id] ? 1 : 0)) || (idx[a.id] - idx[b.id]);
    });
    var nsWait = !nsBat && rest.length ? rest.shift() : null;
    put(striker, "strike", G.strike);
    put(defender, "defend", G.strike);
    put(nsBat, "nsbat", G.ns);
    put(nsWait, "ns", G.ns);
    working.slice(2).forEach(function (a, i) { put(a, "pad", spot(G.pad, i, 126), { offset: -(1.3 + i * 1.7).toFixed(2) }); });
    put(bowler, "bowl", G.mark);
    reviewers.filter(function (r) { return r !== bowler; }).forEach(function (a, i) {
      var p = spot(G.close, i, 22); put(a, "close", p, { walk: walkTo(p, 9) });
    });
    rest.forEach(function (a, i) { var p = spot(G.field, i, 40); put(a, "field", p, { walk: walkTo(p, 18) }); });
    by("free").forEach(function (a) {
      var i = idx[a.id];   // a seat per agent, so the bench never reshuffles
      put(a, "sit", [G.seatX + i * G.seatDX, G.seatY], { seat: i, offset: i % 3 === 2 ? 0 : -(i * 1.9).toFixed(2) });
    });
    by("blocked").forEach(function (a, i) { put(a, "block", spot(G.block, i, 40)); });
    return { map: out, pair: pair.length === 2 && !lock ? [pair[0].id, pair[1].id] : null, targets: targets };
  }

  function nameOf(id) {
    var a = (st.model.agents || []).filter(function (x) { return x.id === id; })[0];
    return a ? a.character.name : st.api.character(id).name;
  }
  function chipFor(a, asg, targets) {
    var t = a.task, id = t ? t.id : "";
    switch (asg.role) {
      case "strike": case "nsbat": case "pad": return [t ? cut(id + " " + (t.title || ""), 22) : "batting", POSE_COLOR.working];
      case "defend": return [cut("in review " + id, 22), POSE_COLOR.reviewing];
      case "bowl": case "close": return [cut("checking " + (id || "work"), 20), POSE_COLOR.reviewing];
      case "ns": case "field": return targets[a.id] ? ["in review", POSE_COLOR.reviewing] : ["waiting", POSE_COLOR.waiting];
      default: return ["", null];
    }
  }

  // ------------------------------------------------------------------ ball
  function buildBall() {
    var S = st.api.svg;
    function chain(parent, withShadow) {
      var g = S("g", { "class": "ck-ball", "aria-hidden": "true" }, parent);
      var x = S("g", { "class": "ck-bx" }, g), y = S("g", { "class": withShadow ? "ck-sy" : "ck-by" }, x);
      var o = S("g", { "class": "ck-bo" }, y);
      if (withShadow) S("ellipse", { cx: 0, cy: 0, rx: 4.4, ry: 1.6, fill: "#00000030" }, o);
      else {
        S("circle", { cx: 0, cy: 0, r: 3.7, fill: "#b3262d", stroke: "#7d1a1f", "stroke-width": 0.6 }, o);
        S("path", { d: "M-2.6,-1.6 q2.6,1.6 5.2,0", fill: "none", stroke: "#f3e3c3", "stroke-width": 0.7 }, o);
        S("circle", { cx: -1.2, cy: -1.3, r: 1, fill: "#ffffff70" }, o);
      }
      return { g: g, x: x };
    }
    st.shadow = chain(st.back, true).g;
    var ball = chain(st.ballLayer, false);
    st.ball = ball.g;
    ball.x.addEventListener("animationiteration", setShot);   // a different shot every delivery
  }
  function setShot() {
    if (!st) return;
    var s = st.defend ? DEFEND : SHOTS[st.shot++ % SHOTS.length];
    [st.ball, st.shadow].forEach(function (g) {
      g.style.setProperty("--ck-mx", s.m[0] + "px"); g.style.setProperty("--ck-my", s.m[1] + "px");
      g.style.setProperty("--ck-ex", s.e[0] + "px"); g.style.setProperty("--ck-ey", s.e[1] + "px");
      g.style.setProperty("--ck-sm", s.sm + "px"); g.style.setProperty("--ck-se", s.se + "px");
    });
  }
  function refreshBall() {
    if (!st || !st.ball) return;
    var bw = null, bat = null;
    Object.keys(st.entries).forEach(function (id) {
      var e = st.entries[id];
      if (!e.pose) return;
      if (e.role === "bowl") bw = e;
      if (e.role === "strike" || e.role === "defend") bat = e;
    });
    var on = !st.api.reducedMotion && !!bw && !!bat;
    st.defend = !!bat && bat.role === "defend";
    if (on === st.ballOn) return;
    st.ballOn = on;
    if (on) setShot();
    [st.ball, st.shadow].forEach(function (g) {
      if (on) g.style.setProperty("--ck-d", clockDelay());
      g.classList.toggle("ck-on", on);
    });
  }

  // two batters run between the wickets just after a shot, every couple of deliveries
  function scheduleSwap() {
    if (!st.pair || st.swapTimer || st.api.reducedMotion) return;
    var t = (((0.905 - phase()) % 1 + 1) % 1) * P;
    if (t < 6) t += P;   // roughly every second delivery
    st.swapTimer = setTimeout(function () {
      if (!st) return;
      st.swapTimer = 0;
      if (!st.pair) return;
      var ready = st.pair.every(function (id) { var e = st.entries[id]; return e && e.pose; });
      if (ready) { st.swapped = !st.swapped; layout(); }
      scheduleSwap();
    }, t * 1000);
  }

  // ------------------------------------------------------------------ scoreboard + celebration
  function statusLine(model) {
    var run = model.run, c = run.counts, ags = model.agents;
    if (run.complete) return "Innings complete: " + c.done + " of " + c.total + " done";
    var rv = ags.filter(function (a) { return a.pose === "reviewing"; })[0];
    if (rv) return cut("Checking " + (rv.task ? rv.task.id : "work") + (rv.target ? " from " + nameOf(rv.target) : ""), 34);
    var bl = ags.filter(function (a) { return a.pose === "blocked"; }).length;
    if (bl) return "Drinks break: " + bl + " blocked";
    if (run.phase === "planning") return "Captain is planning the innings";
    var w = ags.filter(function (a) { return a.pose === "working"; }).length;
    if (w) return w + (w === 1 ? " batter" : " batters") + " at the crease";
    return run.alive ? "Between overs" : "Waiting for play";
  }
  function updateBoard(model) {
    var b = st.board, c = model.run.counts;
    var team = cut(model.run.name || "OmniCLI XI", 26);
    if (b.team.textContent !== team) b.team.textContent = team;
    var score = c.done + "/" + c.failed;
    if (b.score.textContent !== score) {
      var had = b.score.textContent !== "";
      b.score.textContent = score;
      if (had && !st.api.reducedMotion) {
        b.scoreG.classList.remove("ck-roll"); void b.scoreG.getBoundingClientRect(); b.scoreG.classList.add("ck-roll");
      }
    }
    var need = Math.max(0, c.total - c.done);
    b.target.textContent = c.total ? "target " + c.total : "target not set";
    b.need.textContent = c.total ? (need ? "need " + need : "level") : "";
    b.root.classList.toggle("ck-is-won", !!model.run.complete);
    var s = statusLine(model);
    if (b.status.textContent !== s) b.status.textContent = s;
  }
  function celebrate(on) {
    if (on === st.celebrating) return;
    st.celebrating = on;
    st.fx.textContent = "";
    if (!on) return;
    var S = st.api.svg, rand = rng(42), i;
    var cols = ["#f2c14e", "#f5f3ec"].concat(st.model.agents.map(function (a) { return a.character.shirt; }));
    var conf = S("g", { "class": "ck-conf" }, st.fx);
    for (i = 0; i < 46; i++) {
      var r = S("rect", { x: (18 + rand() * 340).toFixed(1), y: (16 + rand() * 240).toFixed(1), width: (3.5 + rand() * 3).toFixed(1),
        height: (6 + rand() * 4).toFixed(1), rx: 1, fill: cols[i % cols.length] }, conf);
      r.style.setProperty("--dx", ((rand() - 0.5) * 70).toFixed(0) + "px");
      r.style.setProperty("--dur", (2.6 + rand() * 1.8).toFixed(2) + "s");
      r.style.setProperty("--del", (rand() * 1.8).toFixed(2) + "s");
    }
    if (st.api.reducedMotion) return;
    [[96, 58, 0, "#f2c14e"], [250, 30, 0.55, "#f5f3ec"], [176, 86, 1.1, "#e8846f"]].forEach(function (f) {
      var at = S("g", { transform: "translate(" + f[0] + "," + f[1] + ")" }, st.fx);
      var b = S("g", { "class": "ck-burst" }, at);
      b.style.setProperty("--del", f[2] + "s");
      for (var k = 0; k < 12; k++) {
        var ang = k * Math.PI / 6, cx = Math.cos(ang), sy = Math.sin(ang);
        S("line", { x1: (cx * 7).toFixed(1), y1: (sy * 7).toFixed(1), x2: (cx * 24).toFixed(1), y2: (sy * 24).toFixed(1),
          stroke: f[3], "stroke-width": 2, "stroke-linecap": "round" }, b);
        S("circle", { cx: (cx * 29).toFixed(1), cy: (sy * 29).toFixed(1), r: 1.6, fill: f[3] }, b);
      }
    });
  }

  // ------------------------------------------------------------------ frame
  function layout() {
    var model = st.model, res = assign(model);
    st.pair = res.pair;
    if (!st.pair && st.swapTimer) { clearTimeout(st.swapTimer); st.swapTimer = 0; }
    model.agents.forEach(function (a) {
      var e = st.entries[a.id], asg = res.map[a.id];
      if (!e || !asg) return;
      setRole(e, asg, a, e.fresh);
      e.fresh = false;
      setTag(e, a.character.name + (a.role === "lead" ? " (c)" : ""));
      e.root.classList.toggle("ck-captain", a.role === "lead");
      var chip = chipFor(a, asg, res.targets);
      setChip(e, chip[0], chip[1]);
      if (asg.role === "block") {
        var b = a.blockers[0] || {};
        e.boardT1.textContent = cut((b.task_id || (a.task && a.task.id) || "task") + " blocked", 14);
        e.boardT2.textContent = cut(b.reason || "drinks break", 17);
      }
    });
    refreshBall();
    scheduleSwap();
    updateBoard(model);
    celebrate(!!model.run.complete);
  }

  OmniOffice.registerScene({
    id: "cricket", label: "Cricket", icon: "🏏",
    mount: function (svg, api) {
      st = { api: api, T0: now(), entries: {}, swapped: false, pairKey: "", pair: null, swapTimer: 0, shot: 0,
             ballOn: false, defend: false, celebrating: false, model: null };
      st.style = api.css(sceneCss());
      var S = api.svg, root = S("g", { "class": "ck-scene" }, svg);
      drawGround(root, S);
      drawPavilion(root, S);
      st.board = drawScoreboard(root, S);
      st.back = S("g", {}, root);                       // ball shadow + umpire, behind the players
      drawUmpire(st.back, api);
      st.people = S("g", { "class": "ck-people" }, root);
      st.ballLayer = S("g", {}, root);
      st.fx = S("g", { "class": "ck-fx", "aria-hidden": "true" }, root);
      buildBall();
    },
    update: function (model, api) {
      if (!st) return;
      st.api = api; st.model = model;
      var seen = {};
      model.agents.forEach(function (a) {
        seen[a.id] = true;
        var e = st.entries[a.id];
        if (!e) { e = st.entries[a.id] = makeEntry(a); e.fresh = true; return; }
        if (e.charKey !== JSON.stringify(a.character)) {      // character picked by the user: redraw in place
          buildPerson(e, a.character);
          e.person.style.setProperty("--ck-d", clockDelay());
        }
      });
      Object.keys(st.entries).forEach(function (id) {
        if (seen[id]) return;
        var e = st.entries[id];
        e.tok++; clearTimeout(e.timer);
        if (e.root.parentNode) e.root.parentNode.removeChild(e.root);
        delete st.entries[id];
      });
      layout();
    },
    unmount: function () {
      if (!st) return;
      Object.keys(st.entries).forEach(function (id) { st.entries[id].tok++; clearTimeout(st.entries[id].timer); });
      clearTimeout(st.swapTimer);
      if (st.style && st.style.parentNode) st.style.parentNode.removeChild(st.style);
      st = null;
    }
  });
})();
