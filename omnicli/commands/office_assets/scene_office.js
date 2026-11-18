/* OmniCLI Office - "Office" scene.
 *
 * A cutaway office seen from the front. Where a person is tells you what they are doing:
 *   at their own desk, typing ........ working
 *   standing at someone else's desk .. checking that person's work (magnifier on their screen)
 *   wandering the floor .............. waiting for someone else
 *   asleep at their desk ............. free
 *   standing at the desk, hand up .... blocked
 * The lead has the glass corner office. Run complete -> confetti, cheer, balloons on the board.
 *
 * Structure: one y-sorted "floor" layer (furniture + people, so nearer things cover farther ones)
 * and one "labels" layer on top (name tags, bubbles, Zzz) that follows each person.
 * Tune timings / positions in CFG; tune motion in the CSS keyframes (all prefixed ofc-).
 */
(function () {
  "use strict";

  var CFG = {
    seatY: 340,                         // feet line of the open-plan desk row
    workerX: [60, 245, 430, 615],       // fixed seat x per worker slot (in order of first appearance)
    lead: { sx: 892, sy: 336, deskY: 352 },
    doorX: 796, officeY: 338,           // glass-office door column and the floor line just inside it
    glassY: 366,                        // front edge of the glass office
    aisleY: 478,                        // horizontal walkway in front of the props
    sit: 13,                            // how far a seated body drops
    standUpMs: 380,                     // pause between standing up and walking off
    wanderPause: [1500, 3200],          // idle time at each spot while waiting
    cheerMs: 2800,                      // celebration length before everyone naps
    confetti: 48
  };

  // Places a waiting person drifts between (feet positions) and what they do there.
  var SPOTS = [
    { id: "cooler", x: 176, y: 484, act: "sip" },
    { id: "coffee", x: 384, y: 486, act: "sip" },
    { id: "printer", x: 556, y: 490, act: "idle" },
    { id: "pace-a", x: 268, y: 530, act: "idle" },
    { id: "pace-b", x: 478, y: 534, act: "idle" },
    { id: "plant", x: 724, y: 530, act: "idle" },
    { id: "lounge", x: 960, y: 532, act: "idle" }
  ];
  var REVIEW_TABLE = { x: 852, y: 490, zone: "front", face: 1 };

  var CSS = [
    ".ofc text{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}",
    ".ofc .ofc-fx{transition:transform .45s cubic-bezier(.2,.8,.2,1)}",
    ".ofc .face-left>.ofc-fx{transform:scaleX(-1)}",
    ".ofc .ofc-agent .p-body,.ofc .ofc-agent .p-leg-l,.ofc .ofc-agent .p-leg-r{transition:transform .38s cubic-bezier(.2,.8,.2,1)}",
    /* sitting: body drops, thighs foreshorten */
    ".ofc .st-sit .p-body{transform:translateY(13px)}",
    ".ofc .st-sit .p-leg-l,.ofc .st-sit .p-leg-r{transform:scaleY(.6)}",
    ".ofc .st-sit .person.is-typing .p-arm-l,.ofc .st-sit .person.is-typing .p-arm-r{transform:rotate(-64deg)}",  /* hands on keys when motion is off */
    /* asleep: lean onto the desk, arms folded, slow breathing */
    ".ofc .st-sleep .p-body{transform-box:view-box;transform-origin:0 -34px;transform:translateY(13px) rotate(38deg);animation:ofc-breathe 3.4s ease-in-out infinite alternate}",
    "@keyframes ofc-breathe{from{transform:translateY(13px) rotate(36deg)}to{transform:translateY(13px) rotate(40deg)}}",
    ".ofc .st-sleep .p-leg-l,.ofc .st-sleep .p-leg-r{transform:rotate(-38deg) scaleY(.6)}",
    ".ofc .st-sleep .p-arm-l{transform:rotate(-122deg)}.ofc .st-sleep .p-arm-r{transform:rotate(-108deg)}",
    ".ofc .st-sleep .p-head{transform:rotate(16deg)}",
    ".ofc .ofc-lids{opacity:0}.ofc .st-sleep .ofc-lids{opacity:1}",
    /* reviewing: arm out with a magnifier on the screen, nodding */
    ".ofc .ofc-mag,.ofc .ofc-alert,.ofc .ofc-cup{display:none}",
    ".ofc .st-review .p-arm-r{transform:rotate(-110deg);animation:ofc-scan 2.8s ease-in-out infinite alternate}",
    "@keyframes ofc-scan{from{transform:rotate(-104deg)}to{transform:rotate(-116deg)}}",
    ".ofc .st-review .ofc-mag{display:inline;transform-box:view-box;transform-origin:15.5px -60px;animation:ofc-scan-prop 2.8s ease-in-out infinite alternate}",
    "@keyframes ofc-scan-prop{from{transform:rotate(6deg)}to{transform:rotate(-6deg)}}",
    ".ofc .st-review .p-head{animation:oo-nod 2.4s ease-in-out infinite}",
    /* blocked: standing, hand up waving a red ! */
    ".ofc .st-blocked .p-arm-r{transform:rotate(-155deg);animation:ofc-wave .9s ease-in-out infinite alternate}",
    "@keyframes ofc-wave{from{transform:rotate(-165deg)}to{transform:rotate(-145deg)}}",
    ".ofc .st-blocked .ofc-alert{display:inline;transform-box:view-box;transform-origin:15.5px -60px;animation:ofc-wave-prop .9s ease-in-out infinite alternate}",
    "@keyframes ofc-wave-prop{from{transform:rotate(-10deg)}to{transform:rotate(10deg)}}",
    /* waiting: sip a drink, look around */
    ".ofc .st-sip .ofc-cup{display:inline;transform-box:view-box;transform-origin:15.5px -60px;animation:ofc-sip-prop 3.6s ease-in-out infinite}",
    ".ofc .st-sip .p-arm-r{animation:ofc-sip 3.6s ease-in-out infinite}",
    "@keyframes ofc-sip{0%,52%,100%{transform:rotate(-8deg)}66%,84%{transform:rotate(118deg)}}",
    "@keyframes ofc-sip-prop{0%,52%,100%{transform:rotate(0)}66%,84%{transform:rotate(126deg)}}",
    ".ofc .st-idle .p-head,.ofc .st-sip .p-head{animation:ofc-look 3.4s ease-in-out infinite}",
    "@keyframes ofc-look{0%,100%{transform:rotate(0)}30%{transform:rotate(-7deg)}65%{transform:rotate(6deg)}}",
    /* celebration */
    ".ofc .st-cheer .p-arm-l{animation:ofc-cheer-l .5s ease-in-out infinite alternate}",
    ".ofc .st-cheer .p-arm-r{animation:ofc-cheer-r .5s ease-in-out infinite alternate}",
    "@keyframes ofc-cheer-l{from{transform:rotate(145deg)}to{transform:rotate(165deg)}}",
    "@keyframes ofc-cheer-r{from{transform:rotate(-145deg)}to{transform:rotate(-165deg)}}",
    ".ofc .st-cheer>.ofc-fx{animation:ofc-hop .5s cubic-bezier(.3,.7,.4,1) infinite alternate}",
    "@keyframes ofc-hop{to{transform:translateY(-9px)}}",
    /* labels */
    ".ofc .ofc-lbl{transition:opacity .3s ease}",
    ".ofc .ofc-lbl-in{transform-box:view-box;transform-origin:0 -100px;transform:scale(var(--ls,1));transition:transform .38s cubic-bezier(.2,.8,.2,1)}",
    ".ofc .ofc-lbl.is-seated .ofc-lbl-in{transform:translateY(13px) scale(var(--ls,1))}",
    "@media (max-width:640px){.ofc{--ls:1.35}}",                     /* keep names legible on phones */
    ".ofc .ofc-lbl .oo-bubble rect{stroke:var(--pc,#0002);stroke-width:1.5}",
    ".ofc .ofc-lbl .oo-bubble path{fill:#fff;stroke:var(--pc,#0002);stroke-width:1.5}",
    ".ofc .ofc-lbl .oo-bubble{animation:ofc-pop .45s cubic-bezier(.34,1.56,.64,1) both;transform-box:fill-box;transform-origin:50% 100%}",
    ".ofc .ofc-lbl.is-blocked .oo-bubble rect{fill:#fff1ee}",
    ".ofc .ofc-lbl.is-blocked .oo-bubble text{fill:#9b2c1f;font-weight:600}",
    ".ofc .ofc-lbl.is-lead .oo-tag rect{fill:#243040;stroke:none}",
    ".ofc .ofc-lbl.is-lead .oo-tag text{fill:#fff}",
    "@keyframes ofc-pop{from{opacity:0;transform:scale(.4)}}",
    ".ofc .ofc-zzz,.ofc .ofc-dots{display:none}",
    ".ofc .ofc-lbl.is-sleep .ofc-zzz,.ofc .ofc-lbl.is-wait .ofc-dots{display:inline}",
    ".ofc .ofc-zzz text{font-weight:800;fill:#5b6b82;stroke:#fffdf8;stroke-width:3px;paint-order:stroke;opacity:.85;animation:ofc-z 3s ease-out infinite;transform-box:fill-box;transform-origin:50% 50%}",
    ".ofc .ofc-zzz .z2{animation-delay:1s}.ofc .ofc-zzz .z3{animation-delay:2s}",
    "@keyframes ofc-z{0%{opacity:0;transform:translate(-10px,16px) scale(.6)}25%{opacity:.9}100%{opacity:0;transform:translate(6px,-14px) scale(1.15)}}",
    ".ofc .ofc-dots circle{fill:var(--waiting);animation:ofc-dot 1.4s ease-in-out infinite}",
    ".ofc .ofc-dots .d2{animation-delay:.2s}.ofc .ofc-dots .d3{animation-delay:.4s}",
    "@keyframes ofc-dot{0%,60%,100%{opacity:.35}30%{opacity:1}}",
    /* monitors */
    ".ofc .mon-code,.ofc .mon-diff,.ofc .mon-alert,.ofc .mon-sleep,.ofc .mon-lock{display:none}",
    ".ofc .m-code .mon-code,.ofc .m-diff .mon-diff,.ofc .m-alert .mon-alert,.ofc .m-sleep .mon-sleep,.ofc .m-lock .mon-lock{display:inline}",
    ".ofc .mon-scroll{animation:ofc-scroll 2.6s steps(8) infinite}",
    "@keyframes ofc-scroll{to{transform:translateY(-40px)}}",
    ".ofc .mon-glow{animation:ofc-flicker 3.3s ease-in-out infinite}",
    "@keyframes ofc-flicker{0%,100%{opacity:.16}40%{opacity:.08}55%{opacity:.2}}",
    ".ofc .mon-scanbar{animation:ofc-scanbar 2.4s ease-in-out infinite alternate}",
    "@keyframes ofc-scanbar{to{transform:translateY(26px)}}",
    ".ofc .mon-alert .bang{animation:ofc-blink 1.2s ease-in-out infinite}",
    "@keyframes ofc-blink{50%{opacity:.45}}",
    ".ofc .mon-saver{animation:ofc-saver 7s ease-in-out infinite alternate}",
    "@keyframes ofc-saver{to{transform:translate(26px,14px)}}",
    /* ambient room */
    ".ofc .cloud{animation:ofc-cloud 70s linear infinite}",
    "@keyframes ofc-cloud{from{transform:translateX(-80px)}to{transform:translateX(250px)}}",
    ".ofc .steam path{animation:ofc-steam 3.6s ease-in-out infinite;transform-box:fill-box;transform-origin:50% 100%}",
    ".ofc .steam .s2{animation-delay:1.8s}",
    "@keyframes ofc-steam{0%{opacity:0;transform:translateY(4px) scaleY(.6)}40%{opacity:.55}100%{opacity:0;transform:translateY(-10px) scaleY(1.1)}}",
    ".ofc .cooler-bubble{animation:ofc-bubble 5s ease-in infinite}",
    ".ofc .cooler-bubble.b2{animation-delay:2.6s}",
    "@keyframes ofc-bubble{0%{opacity:0;transform:translateY(0)}15%{opacity:.8}70%{opacity:.6}100%{opacity:0;transform:translateY(-26px)}}",
    ".ofc .note{transform-box:fill-box;transform-origin:50% 0}",
    ".ofc .note.is-new{animation:ofc-pop .5s cubic-bezier(.34,1.56,.64,1) both}",
    ".ofc .balloons{display:none}.ofc.is-complete .balloons{display:inline}",
    ".ofc .balloon{transform-box:view-box;animation:ofc-sway 4s ease-in-out infinite alternate}",
    ".ofc .balloon.b2{animation-duration:4.8s;animation-delay:-1.2s}.ofc .balloon.b3{animation-duration:5.4s;animation-delay:-2.5s}",
    "@keyframes ofc-sway{from{transform:rotate(-4deg)}to{transform:rotate(4deg)}}",
    ".ofc .done-ribbon{display:none}.ofc.is-complete .done-ribbon{display:inline}",
    ".ofc .ofc-conf{animation:ofc-fall var(--d) cubic-bezier(.25,.6,.45,1) var(--delay) both}",
    "@keyframes ofc-fall{from{transform:translate(var(--x),-24px) rotate(0)}to{transform:translate(calc(var(--x) + var(--dx)),580px) rotate(var(--r))}}"
  ].join("\n");

  // ------------------------------------------------------------------ state
  var A = null;                       // api handed to mount()
  var root, bgL, floorL, labelL, topL, styleEl, clockEls, board, tickTimer;
  var people = {}, desks = [], leadDesk = null, slotOf = {}, nextWorkerSlot = 0, leadId = null;
  var timers = [], alive = false, wasComplete = false, celebrateUntil = 0, uid = 0;

  function S(tag, attrs, parent) { return A.svg(tag, attrs || {}, parent); }
  function T(attrs, parent, text) { var n = S("text", attrs, parent); n.textContent = text; return n; }
  function later(fn, ms, owner) {
    var id = setTimeout(function () {
      timers = timers.filter(function (t) { return t !== id; });
      if (owner) owner.timers = owner.timers.filter(function (t) { return t !== id; });
      if (alive) fn();
    }, ms);
    timers.push(id);
    if (owner) owner.timers.push(id);
    return id;
  }
  function clearOwn(st) { st.timers.forEach(clearTimeout); timers = timers.filter(function (t) { return st.timers.indexOf(t) < 0; }); st.timers = []; }
  function hash(s) { var h = 7; for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0; return h; }
  function rand(a, b) { return a + Math.random() * (b - a); }

  // Items in the floor layer are drawn in order of their feet line (data-k): nearer covers farther.
  function setKey(g, k) { g.setAttribute("data-k", k); }
  function sortFloor() {
    var kids = Array.prototype.slice.call(floorL.children);
    var sorted = kids.slice().sort(function (a, b) { return (+a.getAttribute("data-k")) - (+b.getAttribute("data-k")); });
    for (var i = 0; i < sorted.length; i++) {
      if (floorL.children[i] !== sorted[i]) floorL.insertBefore(sorted[i], floorL.children[i] || null);
    }
  }
  function floorItem(x, y, k) {
    var g = S("g", { transform: "translate(" + x + "," + y + ")" }, floorL);
    setKey(g, k === undefined ? y : k);
    return g;
  }

  // ------------------------------------------------------------------ the room
  function skyGradient(defs, id) {
    var lg = S("linearGradient", { id: id, x1: 0, y1: 0, x2: 0, y2: 1 }, defs);
    S("stop", { offset: "0", "stop-color": "#b9d9ec" }, lg);
    S("stop", { offset: ".62", "stop-color": "#e6eef1" }, lg);
    S("stop", { offset: "1", "stop-color": "#f5e2c6" }, lg);
  }
  function skyline(g, x, w, base) {
    var blocks = [[0, 34, 26], [28, 52, 22], [52, 26, 30], [84, 60, 20], [106, 38, 28], [136, 46, 24], [162, 30, 34]];
    blocks.forEach(function (b, i) {
      if (b[0] + b[2] > w) return;
      S("rect", { x: x + b[0], y: base - b[1], width: b[2], height: b[1], fill: i % 2 ? "#9fb3c4" : "#b3c4d2" }, g);
      for (var r = base - b[1] + 6; r < base - 6; r += 9) {
        if ((r + i * 3) % 4 === 0) S("rect", { x: x + b[0] + 5, y: r, width: 4, height: 3, fill: "#fff6d6" }, g);
      }
    });
  }

  function drawRoom(defs) {
    var g = bgL;
    S("rect", { x: 0, y: 0, width: 1000, height: 240, fill: "#f4ecdf" }, g);
    S("rect", { x: 0, y: 206, width: 770, height: 28, fill: "#ece1cf" }, g);
    S("rect", { x: 0, y: 204, width: 770, height: 3, fill: "#dccbb0" }, g);
    S("rect", { x: 0, y: 232, width: 1000, height: 9, fill: "#d9c7a8" }, g);
    // oak floor
    S("rect", { x: 0, y: 240, width: 1000, height: 320, fill: "#dcc19a" }, g);
    for (var y = 258, row = 0; y < 560; y += 21, row++) {
      S("rect", { x: 0, y: y, width: 1000, height: 1.2, fill: "#c9aa7e", opacity: .55 }, g);
      for (var x = (row * 137) % 190; x < 1000; x += 190) S("rect", { x: x, y: y - 21, width: 1.2, height: 21, fill: "#c9aa7e", opacity: .4 }, g);
    }
    S("ellipse", { cx: 912, cy: 500, rx: 96, ry: 26, fill: "#e9d8bb" }, g);           // lounge rug
    S("ellipse", { cx: 912, cy: 500, rx: 80, ry: 19, fill: "none", stroke: "#d8c19c", "stroke-width": 2 }, g);

    // window with a slow cloud
    skyGradient(defs, "ofc-sky");
    var clip = S("clipPath", { id: "ofc-win" }, defs);
    S("rect", { x: 32, y: 38, width: 200, height: 144 }, clip);
    S("rect", { x: 22, y: 28, width: 220, height: 164, rx: 5, fill: "#fbf7ef", stroke: "#d8c8ad" }, g);
    var view = S("g", { "clip-path": "url(#ofc-win)" }, g);
    S("rect", { x: 32, y: 38, width: 200, height: 144, fill: "url(#ofc-sky)" }, view);
    var cloud = S("g", { "class": "cloud" }, view);
    [[70, 70, 22, 9], [88, 64, 16, 10], [104, 71, 18, 8], [170, 96, 16, 6], [184, 92, 12, 7]].forEach(function (c) {
      S("ellipse", { cx: c[0], cy: c[1], rx: c[2], ry: c[3], fill: "#ffffff", opacity: .85 }, cloud);
    });
    skyline(view, 34, 196, 182);
    S("rect", { x: 130, y: 38, width: 4, height: 144, fill: "#fbf7ef" }, g);
    S("rect", { x: 32, y: 108, width: 200, height: 4, fill: "#fbf7ef" }, g);
    S("rect", { x: 16, y: 190, width: 232, height: 7, rx: 2, fill: "#e6d7bd" }, g);

    // clock (real local time)
    var ck = S("g", { transform: "translate(281,76)" }, g);
    S("circle", { r: 25, fill: "#fffdf8", stroke: "#3a4556", "stroke-width": 3 }, ck);
    for (var i = 0; i < 12; i++) {
      var a = i * Math.PI / 6;
      S("line", { x1: Math.sin(a) * 18, y1: -Math.cos(a) * 18, x2: Math.sin(a) * 21, y2: -Math.cos(a) * 21,
                  stroke: "#3a4556", "stroke-width": i % 3 ? 1 : 2 }, ck);
    }
    clockEls = {
      h: S("line", { x1: 0, y1: 3, x2: 0, y2: -11, stroke: "#243040", "stroke-width": 3, "stroke-linecap": "round" }, ck),
      m: S("line", { x1: 0, y1: 4, x2: 0, y2: -17, stroke: "#243040", "stroke-width": 2, "stroke-linecap": "round" }, ck),
      s: S("line", { x1: 0, y1: 5, x2: 0, y2: -19, stroke: "#d4614f", "stroke-width": 1 }, ck)
    };
    S("circle", { r: 2, fill: "#243040" }, ck);

    // shelf + framed print
    S("rect", { x: 664, y: 46, width: 70, height: 58, rx: 3, fill: "#fffdf8", stroke: "#cdbfa6", "stroke-width": 3 }, g);
    S("circle", { cx: 686, cy: 76, r: 13, fill: "#e0a23b", opacity: .8 }, g);
    S("circle", { cx: 708, cy: 70, r: 10, fill: "#2f9e6f", opacity: .7 }, g);
    S("rect", { x: 696, y: 80, width: 26, height: 12, fill: "#8a63d2", opacity: .55 }, g);
    S("rect", { x: 650, y: 140, width: 100, height: 5, rx: 2, fill: "#c9a77a" }, g);
    [["#4c7fe6", 8, 30], ["#d4614f", 7, 26], ["#2f9e6f", 9, 32], ["#e0a23b", 6, 24]].forEach(function (b, k) {
      S("rect", { x: 658 + k * 10, y: 140 - b[2], width: b[1], height: b[2], rx: 1, fill: b[0] }, g);
    });
    S("rect", { x: 712, y: 126, width: 16, height: 14, rx: 3, fill: "#f1ede6", stroke: "#d6cbbb" }, g);
    S("path", { d: "M720,126 q-8,-12 -2,-18 q4,8 2,18 M720,126 q8,-14 4,-20 q-4,10 -4,20", fill: "#5f9e6e" }, g);

    drawOfficeBack(defs);
  }

  // Glass corner office: back wall, window with a view, carpet (behind the lead).
  function drawOfficeBack(defs) {
    var g = bgL;
    S("rect", { x: 770, y: 96, width: 230, height: 138, fill: "#e6ebef" }, g);
    S("rect", { x: 770, y: 232, width: 230, height: 9, fill: "#cdd5dc" }, g);
    S("rect", { x: 770, y: 240, width: 230, height: 126, fill: "#c8d0d8" }, g);
    for (var y = 252; y < 366; y += 14) S("rect", { x: 770, y: y, width: 230, height: 1, fill: "#bdc6cf" }, g);
    var clip = S("clipPath", { id: "ofc-win2" }, defs);
    S("rect", { x: 884, y: 118, width: 98, height: 76 }, clip);
    S("rect", { x: 878, y: 112, width: 110, height: 88, rx: 4, fill: "#f6f8fa", stroke: "#c6cfd8" }, g);
    var view = S("g", { "clip-path": "url(#ofc-win2)" }, g);
    S("rect", { x: 884, y: 118, width: 98, height: 76, fill: "url(#ofc-sky)" }, view);
    skyline(view, 880, 104, 194);
    S("rect", { x: 931, y: 118, width: 3, height: 76, fill: "#f6f8fa" }, g);
    // framed award
    S("rect", { x: 800, y: 122, width: 50, height: 38, rx: 2, fill: "#fffdf8", stroke: "#b9975a", "stroke-width": 3 }, g);
    S("circle", { cx: 825, cy: 136, r: 6, fill: "#e0a23b" }, g);
    S("rect", { x: 810, y: 147, width: 30, height: 2, fill: "#c9b48a" }, g);
    S("rect", { x: 814, y: 152, width: 22, height: 2, fill: "#c9b48a" }, g);
  }

  // The glass front sits in the floor layer so people inside are behind it and people outside in front.
  function drawGlassFront(leadName) {
    var g = floorItem(0, 0, CFG.glassY);
    var frame = "#8f9cab";
    S("rect", { x: 830, y: 96, width: 170, height: 270, fill: "#d3ebf6", opacity: .26 }, g);
    S("path", { d: "M902,104 l24,0 l-52,252 l-24,0z M952,104 l9,0 l-52,252 l-9,0z", fill: "#ffffff", opacity: .24 }, g);
    S("rect", { x: 772, y: 100, width: 58, height: 112, fill: "#d3ebf6", opacity: .26 }, g);   // transom
    S("rect", { x: 832, y: 102, width: 164, height: 18, fill: "#ffffff", opacity: .55 }, g);    // frosted band
    var decal = T({ x: 914, y: 115, "text-anchor": "middle", "font-size": 11, "font-weight": 700, fill: "#5d6b7c",
                    "letter-spacing": ".04em" }, g, leadName ? leadName + ", lead" : "Lead office");
    S("rect", { x: 766, y: 92, width: 234, height: 8, fill: frame }, g);
    S("rect", { x: 768, y: 100, width: 4, height: 266, fill: frame }, g);
    S("rect", { x: 828, y: 100, width: 4, height: 266, fill: frame }, g);
    S("rect", { x: 996, y: 100, width: 4, height: 266, fill: frame }, g);
    S("rect", { x: 772, y: 210, width: 56, height: 4, fill: frame }, g);                       // door header
    S("rect", { x: 830, y: 362, width: 170, height: 4, fill: frame }, g);
    S("rect", { x: 820, y: 278, width: 3, height: 26, rx: 1.5, fill: "#5d6b7c" }, g);          // door handle
    return decal;
  }

  // Live task board: To do / Doing / Done notes come from run.counts.
  function drawBoard() {
    var g = S("g", {}, bgL);
    S("rect", { x: 316, y: 30, width: 308, height: 170, rx: 7, fill: "#fbfaf6", stroke: "#cdbfa6", "stroke-width": 3 }, g);
    var title = T({ x: 330, y: 52, "font-size": 13, "font-weight": 700, fill: "#243040" }, g, "Task board");
    var cols = [], names = ["To do", "Doing", "Done"];
    for (var i = 0; i < 3; i++) {
      var x = 324 + i * 100;
      if (i) S("rect", { x: x - 4, y: 62, width: 1.5, height: 130, fill: "#e5dccd" }, g);
      var head = T({ x: x + 4, y: 76, "font-size": 11, "font-weight": 600, fill: "#6b7686" }, g, names[i]);
      cols.push({ x: x, head: head, name: names[i], notes: S("g", {}, g), n: -1, f: -1 });
    }
    var ribbon = S("g", { "class": "done-ribbon" }, g);
    S("rect", { x: 404, y: 150, width: 132, height: 26, rx: 13, fill: "#2f9e6f" }, ribbon);
    T({ x: 470, y: 167, "text-anchor": "middle", "font-size": 12, "font-weight": 700, fill: "#fff" }, ribbon, "All tasks done");
    var balloons = S("g", { "class": "balloons" }, g);
    board = { title: title, cols: cols, balloons: balloons };
  }
  function drawBalloons(colors) {
    var g = board.balloons;
    g.textContent = "";
    var tie = [616, 62];
    [[588, 12, "b1"], [614, 4, "b2"], [640, 14, "b3"]].forEach(function (b, i) {
      var bg = S("g", { "class": "balloon " + b[2], style: "transform-origin:" + tie[0] + "px " + tie[1] + "px" }, g);
      S("path", { d: "M" + tie[0] + "," + tie[1] + " Q" + ((tie[0] + b[0]) / 2 + 6) + "," + (tie[1] - 12) + " " + b[0] + "," + (b[1] + 32),
                  fill: "none", stroke: "#8a97a8", "stroke-width": 1 }, bg);
      S("ellipse", { cx: b[0], cy: b[1] + 16, rx: 13, ry: 16, fill: colors[i % colors.length] }, bg);
      S("ellipse", { cx: b[0] - 4, cy: b[1] + 10, rx: 3, ry: 5, fill: "#ffffff", opacity: .45 }, bg);
      S("path", { d: "M" + (b[0] - 3) + "," + (b[1] + 34) + " l3,-3 l3,3z", fill: colors[i % colors.length] }, bg);
    });
  }
  var NOTE = { "To do": "#fde68a", "Doing": "#bfdbfe", "Done": "#bbf7d0" };
  function renderBoard(run) {
    var c = run.counts;
    var todo = c.pending || Math.max(0, c.total - c.done - c.running - c.failed);
    board.title.textContent = (run.name || "Task board").slice(0, 34);
    [[todo, 0], [c.running, 0], [c.done, c.failed]].forEach(function (v, i) {
      var col = board.cols[i], n = Math.max(0, v[0] | 0), f = Math.max(0, v[1] | 0);
      col.head.textContent = col.name + "  " + (n + f);
      if (col.n === n && col.f === f) return;
      var grow = col.n >= 0 && n + f > col.n + col.f;
      col.n = n; col.f = f;
      col.notes.textContent = "";
      var total = Math.min(n + f, 12);
      for (var k = 0; k < total; k++) {
        var nx = col.x + 2 + (k % 3) * 30, ny = 84 + Math.floor(k / 3) * 25;
        var cls = "note" + (grow && k === total - 1 && !A.reducedMotion ? " is-new" : "");
        var note = S("g", { "class": cls }, col.notes);
        S("rect", { x: nx, y: ny, width: 26, height: 21, rx: 2, fill: k < n ? NOTE[col.name] : "#fecaca",
                    transform: "rotate(" + ((k * 7) % 5 - 2) + " " + (nx + 13) + " " + ny + ")" }, note);
        S("rect", { x: nx + 4, y: ny + 7, width: 16, height: 2, fill: "#00000022" }, note);
        S("rect", { x: nx + 4, y: ny + 12, width: 11, height: 2, fill: "#00000022" }, note);
        S("circle", { cx: nx + 13, cy: ny + 2, r: 1.8, fill: "#d4614f" }, note);
      }
      if (n + f > 12) T({ x: col.x + 88, y: 192, "text-anchor": "end", "font-size": 10, fill: "#6b7686" }, col.notes, "+" + (n + f - 12));
    });
  }

  // ------------------------------------------------------------------ furniture
  var defsEl, tableScreen;
  var CODE = [[0, 22, "#7dd3fc"], [4, 30, "#e2e8f0"], [4, 18, "#c4b5fd"], [8, 26, "#86efac"],
              [8, 14, "#fcd34d"], [4, 24, "#e2e8f0"], [0, 12, "#f9a8d4"], [0, 0, ""]];

  // A monitor screen with every mode pre-built; setScreen() just swaps a class.
  function buildScreen(parent, x, y, w, h) {
    var id = "ofc-scr-" + (++uid);
    S("rect", { x: x, y: y, width: w, height: h }, S("clipPath", { id: id }, defsEl));
    var g = S("g", { "class": "screen m-off", "clip-path": "url(#" + id + ")" }, parent);
    S("rect", { x: x, y: y, width: w, height: h, fill: "#1b2430" }, g);
    var code = S("g", { "class": "mon-code" }, g), sc = S("g", { "class": "mon-scroll" }, code), i;
    for (i = 0; i < 16; i++) {
      var c = CODE[i % 8];
      if (c[1]) S("rect", { x: x + 3 + c[0], y: y + 3 + i * 5, width: Math.min(c[1], w - 6 - c[0]), height: 2.2, rx: 1, fill: c[2] }, sc);
    }
    S("rect", { "class": "mon-glow", x: x, y: y, width: w, height: h, fill: "#9ff3cf", opacity: .14 }, code);
    var diff = S("g", { "class": "mon-diff" }, g);
    for (i = 0; i < 7; i++) {
      var tint = i % 3 === 1 ? "#ef444470" : i % 3 === 2 ? "#22c55e70" : null;
      if (tint) S("rect", { x: x, y: y + 1.5 + i * 4.6, width: w, height: 4, fill: tint }, diff);
      S("rect", { x: x + 4, y: y + 2.5 + i * 4.6, width: (w - 8) * (0.4 + ((i * 37) % 5) / 10), height: 2, rx: 1, fill: "#e2e8f0", opacity: .85 }, diff);
    }
    S("rect", { "class": "mon-scanbar", x: x, y: y + 1, width: w, height: 6, fill: "#a78bfa", opacity: .4 }, diff);
    var al = S("g", { "class": "mon-alert" }, g);
    S("rect", { x: x, y: y, width: w, height: h, fill: "#8f2a1e" }, al);
    T({ "class": "bang", x: x + w / 2, y: y + h / 2 + 8, "text-anchor": "middle", "font-size": 22, "font-weight": 800, fill: "#ffe4de" }, al, "!");
    var sl = S("g", { "class": "mon-sleep" }, g);
    S("rect", { x: x, y: y, width: w, height: h, fill: "#10151d" }, sl);
    var sv = S("g", { "class": "mon-saver" }, sl);
    S("circle", { cx: x + 9, cy: y + 9, r: 3.4, fill: "#7b8aa0" }, sv);
    S("circle", { cx: x + 10.6, cy: y + 7.8, r: 2.8, fill: "#10151d" }, sv);
    var lk = S("g", { "class": "mon-lock" }, g);
    S("rect", { x: x, y: y, width: w, height: h, fill: "#1e2633" }, lk);
    S("rect", { x: x + w / 2 - 5, y: y + h / 2 - 2, width: 10, height: 8, rx: 1.5, fill: "#64748b" }, lk);
    S("path", { d: "M" + (x + w / 2 - 3) + "," + (y + h / 2 - 2) + " v-3 a3,3 0 0 1 6,0 v3", fill: "none", stroke: "#64748b", "stroke-width": 1.6 }, lk);
    return g;
  }
  function setScreen(scr, mode) { var c = "screen m-" + mode; if (scr.getAttribute("class") !== c) scr.setAttribute("class", c); }

  function deskProp(g, kind) {
    if (kind === "mug") {
      S("path", { d: "M121,-47 q5,0 5,3 q0,3 -5,3", fill: "none", stroke: "#cbbfa9", "stroke-width": 1.5 }, g);
      S("rect", { x: 113, y: -49, width: 9, height: 9, rx: 1.5, fill: "#fffdf8", stroke: "#cbbfa9", "stroke-width": .8 }, g);
    } else if (kind === "plant") {
      S("path", { d: "M118,-50 q-7,-9 -2,-15 q4,7 2,15 M118,-50 q7,-10 3,-17 q-3,9 -3,17", fill: "#5f9e6e" }, g);
      S("path", { d: "M112,-50 h12 l-2,9 h-8z", fill: "#e8e2d6", stroke: "#cfc4b2", "stroke-width": .6 }, g);
    } else if (kind === "papers") {
      S("rect", { x: 109, y: -44, width: 18, height: 3, fill: "#fffdf8", stroke: "#d9cfbf", "stroke-width": .6 }, g);
      S("rect", { x: 111, y: -47, width: 17, height: 3, fill: "#f7f3ea", stroke: "#d9cfbf", "stroke-width": .6, transform: "rotate(-5 119 -45)" }, g);
      S("rect", { x: 104, y: -93, width: 7, height: 7, fill: "#fde68a", transform: "rotate(8 107 -90)" }, g);
    } else {
      S("ellipse", { cx: 122, cy: -41.5, rx: 6, ry: 1.6, fill: "#3a4252" }, g);
      S("path", { d: "M122,-42 L116,-62 L126,-74", stroke: "#3a4252", "stroke-width": 2, fill: "none" }, g);
      S("path", { d: "M120,-78 l14,3 l-3,8 l-14,-3z", fill: "#2f9e6f" }, g);
    }
  }

  var DESK_PROPS = ["mug", "plant", "papers", "lamp"];
  function drawWorkerDesk(slot, sx, sy) {
    var g = floorItem(sx, sy, sy - 2);
    S("ellipse", { cx: 62, cy: 0, rx: 74, ry: 5, fill: "#00000014" }, g);
    // chair: the person sits in front of the backrest
    S("rect", { x: -15, y: -66, width: 30, height: 42, rx: 9, fill: "#3f4a5c" }, g);
    S("rect", { x: -11, y: -62, width: 22, height: 30, rx: 7, fill: "#4a566a" }, g);
    S("rect", { x: -2, y: -20, width: 4, height: 15, fill: "#6b7685" }, g);
    S("path", { d: "M-15,-5 L15,-5", stroke: "#4b5565", "stroke-width": 3, "stroke-linecap": "round" }, g);
    [-15, 0, 15].forEach(function (cx) { S("circle", { cx: cx, cy: -2, r: 2.2, fill: "#2a313c" }, g); });
    S("rect", { x: -18, y: -26, width: 36, height: 7, rx: 3, fill: "#354052" }, g);
    // desk
    S("rect", { x: 20, y: -32, width: 4, height: 32, fill: "#b9976a" }, g);
    S("rect", { x: 24, y: -31, width: 72, height: 17, fill: "#e3c79c" }, g);
    S("rect", { x: 96, y: -32, width: 31, height: 32, rx: 2, fill: "#d9bb8e" }, g);
    S("rect", { x: 99, y: -27, width: 25, height: 11, rx: 1.5, fill: "#e6cba0" }, g);
    S("rect", { x: 99, y: -14, width: 25, height: 11, rx: 1.5, fill: "#e6cba0" }, g);
    S("rect", { x: 108, y: -22.5, width: 7, height: 2, rx: 1, fill: "#9c7b52" }, g);
    S("rect", { x: 108, y: -9.5, width: 7, height: 2, rx: 1, fill: "#9c7b52" }, g);
    S("path", { d: "M14,-34 L132,-34 L127,-41 L19,-41 Z", fill: "#efdcbb" }, g);
    S("rect", { x: 14, y: -34, width: 118, height: 4, fill: "#d4b183" }, g);
    // name plate: whose desk this is, even when they are away (hidden until someone claims it)
    var plate = S("g", { style: "display:none" }, g);
    S("rect", { x: 30, y: -28, width: 60, height: 11, rx: 2, fill: "#fffaf0", stroke: "#cfb38a", "stroke-width": .8 }, plate);
    var chip = S("rect", { x: 30, y: -28, width: 4, height: 11, rx: 1, fill: "#d8cbb5" }, plate);
    var name = T({ x: 62, y: -19.8, "text-anchor": "middle", "font-size": 8, "font-weight": 700, fill: "#4a3b2a" }, plate, "");
    // keyboard, mouse, monitor
    S("rect", { x: 27, y: -40, width: 30, height: 4, rx: 1, fill: "#f8f6f2", stroke: "#c9c3b8", "stroke-width": .6 }, g);
    S("ellipse", { cx: 63, cy: -38, rx: 3, ry: 1.8, fill: "#f8f6f2", stroke: "#c9c3b8", "stroke-width": .6 }, g);
    S("rect", { x: 79, y: -53, width: 6, height: 13, fill: "#4b5565" }, g);
    S("ellipse", { cx: 82, cy: -40.5, rx: 10, ry: 2, fill: "#3b4453" }, g);
    S("rect", { x: 56, y: -94, width: 52, height: 42, rx: 3, fill: "#283241" }, g);
    var scr = buildScreen(g, 59, -91, 46, 34);
    var led = S("circle", { cx: 103.5, cy: -54.5, r: 1.6, fill: "#64748b" }, g);
    deskProp(g, DESK_PROPS[slot % 4]);
    return { g: g, scr: scr, led: led, name: name, chip: chip, plate: plate, owner: null,
             seat: { x: sx, y: sy, zone: "desk" }, review: { x: sx + 120, y: sy + 8, zone: "desk", face: -1 } };   // in front of the drawers, beside the screen
  }

  // Executive desk, seen from the front: the lead sits behind it.
  function drawLeadDesk() {
    var L = CFG.lead;
    var chair = floorItem(L.sx, L.sy, L.sy - 6);
    S("rect", { x: -19, y: -88, width: 38, height: 62, rx: 11, fill: "#2d3440" }, chair);
    S("rect", { x: -14, y: -83, width: 28, height: 50, rx: 8, fill: "#384150" }, chair);
    S("rect", { x: -20, y: -27, width: 40, height: 7, rx: 3, fill: "#2a303b" }, chair);
    var g = floorItem(L.sx, L.deskY, L.deskY);
    S("ellipse", { cx: 0, cy: 0, rx: 82, ry: 5, fill: "#00000018" }, g);
    S("rect", { x: -70, y: -46, width: 140, height: 46, fill: "#74513a" }, g);
    S("rect", { x: -62, y: -39, width: 48, height: 33, rx: 2, fill: "#6b4a33" }, g);
    S("rect", { x: 14, y: -39, width: 48, height: 33, rx: 2, fill: "#6b4a33" }, g);
    S("path", { d: "M-75,-48 L75,-48 L68,-57 L-68,-57 Z", fill: "#8a6243" }, g);
    S("rect", { x: -75, y: -48, width: 150, height: 5, fill: "#5e4130" }, g);
    S("rect", { x: -32, y: -31, width: 64, height: 13, rx: 2, fill: "#d9b45a" }, g);
    var name = T({ x: 0, y: -21.5, "text-anchor": "middle", "font-size": 8.5, "font-weight": 800, fill: "#4a3510" }, g, "");
    S("rect", { x: 6, y: -56, width: 34, height: 4, rx: 1, fill: "#e9e6df" }, g);
    S("rect", { x: -41, y: -67, width: 6, height: 11, fill: "#4b5565" }, g);
    S("ellipse", { cx: -38, cy: -56.5, rx: 11, ry: 2, fill: "#3b4453" }, g);
    S("rect", { x: -64, y: -102, width: 52, height: 38, rx: 3, fill: "#283241" }, g);
    var scr = buildScreen(g, -61, -99, 46, 31);
    var led = S("circle", { cx: -16.5, cy: -66.5, r: 1.6, fill: "#64748b" }, g);
    S("rect", { x: 52, y: -66, width: 9, height: 10, rx: 2, fill: "#3a4252" }, g);
    S("path", { d: "M56,-60 L50,-80 L60,-92", stroke: "#3a4252", "stroke-width": 2.2, fill: "none" }, g);
    S("path", { d: "M54,-97 l16,4 l-4,9 l-16,-4z", fill: "#e0a23b" }, g);
    S("rect", { x: 26, y: -64, width: 8, height: 8, rx: 1.5, fill: "#fffdf8", stroke: "#cbbfa9", "stroke-width": .8 }, g);
    return { g: g, scr: scr, led: led, name: name, chip: null, owner: null, lead: true,
             seat: { x: L.sx, y: L.sy, zone: "office" }, review: { x: CFG.doorX, y: L.sy + 8, zone: "office", face: 1 } };
  }

  function drawProps() {
    // water cooler
    var c = floorItem(138, 468);
    S("ellipse", { cx: 0, cy: 0, rx: 20, ry: 4, fill: "#00000016" }, c);
    S("rect", { x: -15, y: -56, width: 30, height: 56, rx: 4, fill: "#eef2f5", stroke: "#c8d0d8" }, c);
    S("rect", { x: -9, y: -44, width: 18, height: 14, rx: 2, fill: "#d5dde4" }, c);
    S("rect", { x: -6, y: -43, width: 4, height: 5, rx: 1, fill: "#5aa7d6" }, c);
    S("rect", { x: 2, y: -43, width: 4, height: 5, rx: 1, fill: "#d4614f" }, c);
    S("rect", { x: -11, y: -27, width: 22, height: 3, rx: 1, fill: "#b9c3cc" }, c);
    S("rect", { x: -8, y: -62, width: 16, height: 6, fill: "#cfd8e0" }, c);
    S("path", { d: "M-13,-62 v-24 q0,-6 6,-6 h14 q6,0 6,6 v24z", fill: "#9fd3f0", opacity: .8, stroke: "#7cbfe4" }, c);
    S("circle", { "class": "cooler-bubble", cx: -4, cy: -66, r: 1.8, fill: "#ffffff" }, c);
    S("circle", { "class": "cooler-bubble b2", cx: 4, cy: -68, r: 1.4, fill: "#ffffff" }, c);
    S("rect", { x: 16, y: -42, width: 7, height: 18, rx: 1, fill: "#f8fafc", stroke: "#cbd5e1" }, c);
    // coffee counter
    var k = floorItem(322, 470);
    S("ellipse", { cx: 0, cy: 0, rx: 52, ry: 4, fill: "#00000016" }, k);
    S("rect", { x: -46, y: -40, width: 92, height: 40, rx: 2, fill: "#c9a77a" }, k);
    S("rect", { x: -42, y: -36, width: 40, height: 32, rx: 2, fill: "#d3b388" }, k);
    S("rect", { x: 2, y: -36, width: 40, height: 32, rx: 2, fill: "#d3b388" }, k);
    S("rect", { x: -6, y: -22, width: 3, height: 9, rx: 1, fill: "#8d6d47" }, k);
    S("rect", { x: 3, y: -22, width: 3, height: 9, rx: 1, fill: "#8d6d47" }, k);
    S("rect", { x: -50, y: -45, width: 100, height: 6, rx: 2, fill: "#ecdcc0" }, k);
    S("rect", { x: -30, y: -82, width: 32, height: 37, rx: 5, fill: "#3a4252" }, k);
    S("rect", { x: -26, y: -78, width: 24, height: 9, rx: 2, fill: "#2b3240" }, k);
    S("circle", { cx: -8, cy: -73.5, r: 1.6, fill: "#2f9e6f" }, k);
    S("rect", { x: -18, y: -64, width: 8, height: 4, fill: "#2b3240" }, k);
    S("rect", { x: -19, y: -54, width: 10, height: 9, rx: 1.5, fill: "#fffdf8" }, k);
    var steam = S("g", { "class": "steam" }, k);
    S("path", { d: "M-16,-57 q-3,-5 0,-9 q3,-4 0,-8", fill: "none", stroke: "#ffffff", "stroke-width": 1.6, "stroke-linecap": "round", opacity: .5 }, steam);
    S("path", { "class": "s2", d: "M-12,-57 q3,-5 0,-9 q-3,-4 0,-8", fill: "none", stroke: "#ffffff", "stroke-width": 1.6, "stroke-linecap": "round", opacity: .5 }, steam);
    S("rect", { x: 14, y: -60, width: 14, height: 15, rx: 3, fill: "#f3ead9", stroke: "#d6c6a8" }, k);
    S("rect", { x: 16, y: -54, width: 10, height: 7, rx: 2, fill: "#6b4a33" }, k);
    // printer
    var p = floorItem(506, 470);
    S("ellipse", { cx: 0, cy: 0, rx: 34, ry: 4, fill: "#00000016" }, p);
    S("rect", { x: -28, y: -34, width: 56, height: 34, rx: 2, fill: "#cfd7df" }, p);
    S("rect", { x: -24, y: -30, width: 48, height: 12, rx: 1.5, fill: "#dbe2e8" }, p);
    S("rect", { x: -24, y: -15, width: 48, height: 12, rx: 1.5, fill: "#dbe2e8" }, p);
    S("rect", { x: -31, y: -58, width: 62, height: 24, rx: 5, fill: "#f1f4f7", stroke: "#c3cbd3" }, p);
    S("rect", { x: -19, y: -63, width: 38, height: 6, rx: 1, fill: "#ffffff", stroke: "#d6dce2" }, p);
    S("rect", { x: -24, y: -46, width: 48, height: 3, rx: 1.5, fill: "#9aa6b2" }, p);
    S("circle", { cx: 22, cy: -52, r: 2, fill: "#2f9e6f" }, p);
    // big plant
    var pl = floorItem(690, 488);
    S("ellipse", { cx: 0, cy: 0, rx: 26, ry: 5, fill: "#00000018" }, pl);
    [[-30, -62, 9, 28, -38], [28, -66, 9, 28, 34], [-18, -88, 8, 30, -18], [16, -92, 8, 30, 16], [0, -102, 8, 30, 0],
     [-34, -84, 7, 20, -55], [34, -84, 7, 20, 55]].forEach(function (l, i) {
      S("ellipse", { cx: l[0], cy: l[1], rx: l[2], ry: l[3], fill: ["#5f9e6e", "#4c8a5c", "#7fb88a"][i % 3],
                     transform: "rotate(" + l[4] + " " + l[0] + " " + l[1] + ")" }, pl);
    });
    S("path", { d: "M-22,-36 h44 l-5,36 h-34z", fill: "#f1ede6", stroke: "#d6cbbb" }, pl);
    S("rect", { x: -21, y: -28, width: 42, height: 4, fill: "#8a63d2", opacity: .45 }, pl);
    // shared review table (used when the reviewed work has no known owner)
    var t = floorItem(912, 478);
    S("ellipse", { cx: 0, cy: 0, rx: 30, ry: 4, fill: "#00000018" }, t);
    S("ellipse", { cx: 0, cy: -2, rx: 17, ry: 3.5, fill: "#8a939e" }, t);
    S("rect", { x: -3, y: -46, width: 6, height: 44, fill: "#9aa3ad" }, t);
    S("ellipse", { cx: 0, cy: -46, rx: 44, ry: 9, fill: "#f1e2c8", stroke: "#d9c3a0" }, t);
    S("path", { d: "M-25,-49 h34 l-3,-4 h-28z", fill: "#c3cbd3" }, t);
    S("rect", { x: -24, y: -77, width: 32, height: 25, rx: 2, fill: "#3a4252" }, t);
    tableScreen = buildScreen(t, -22, -75, 28, 20);
    setScreen(tableScreen, "lock");
    S("rect", { x: 14, y: -53, width: 18, height: 5, fill: "#fffdf8", stroke: "#d9cfbf", "stroke-width": .6, transform: "rotate(-8 23 -50)" }, t);
    var stool = floorItem(972, 470);
    S("rect", { x: -2, y: -26, width: 4, height: 24, fill: "#9aa3ad" }, stool);
    S("ellipse", { cx: 0, cy: -2, rx: 10, ry: 2.5, fill: "#8a939e" }, stool);
    S("ellipse", { cx: 0, cy: -28, rx: 14, ry: 5, fill: "#4c7fe6", opacity: .85 }, stool);
  }

  // ------------------------------------------------------------------ people
  var POSE_VAR = { working: "var(--working)", reviewing: "var(--reviewing)", waiting: "var(--waiting)",
                   free: "var(--free)", blocked: "var(--blocked)" };
  var taken = {};                     // wander spot id -> agent id
  var glassDecal, hotDesks = 0;

  // Scene props live in the rig's .p-prop group (person coordinates); CSS shows the one a pose needs.
  function addProps(person, ch) {
    var prop = person.querySelector(".p-prop");
    var mag = S("g", { "class": "ofc-mag" }, prop);
    S("line", { x1: 40, y1: -69, x2: 46.5, y2: -72, stroke: "#3a4252", "stroke-width": 2.6, "stroke-linecap": "round" }, mag);
    S("circle", { cx: 52, cy: -75, r: 6.5, fill: "#dff3fb", "fill-opacity": .55, stroke: "#3a4252", "stroke-width": 2 }, mag);
    S("path", { d: "M48.5,-77 a4,4 0 0 1 3,-2.5", fill: "none", stroke: "#ffffff", "stroke-width": 1.4, "stroke-linecap": "round" }, mag);
    var al = S("g", { "class": "ofc-alert" }, prop);
    S("line", { x1: 27, y1: -85, x2: 28.5, y2: -89, stroke: "#7c2d12", "stroke-width": 2 }, al);
    S("circle", { cx: 29, cy: -96, r: 8, fill: "#d4614f", stroke: "#ffffff", "stroke-width": 1.5 }, al);
    T({ x: 29, y: -91.5, "text-anchor": "middle", "font-size": 12, "font-weight": 800, fill: "#ffffff" }, al, "!");
    var cup = S("g", { "class": "ofc-cup" }, prop);
    S("path", { d: "M15.5,-38 h8 l-1,9 h-6z", fill: "#fffdf8", stroke: "#94a3b8", "stroke-width": .8 }, cup);
    // closed eyes for sleeping, drawn over the rig's eyes in the head's own colours
    var head = person.querySelector(".p-head");
    if (head) {
      var lids = S("g", { "class": "ofc-lids" }, head), ex = ch.accessory === "glasses" ? 4.5 : 4;
      [-ex, ex].forEach(function (x) {
        S("circle", { cx: x, cy: -77, r: 2.1, fill: ch.skin }, lids);
        S("path", { d: "M" + (x - 2) + ",-76.8 q2,1.6 4,0", fill: "none", stroke: "#1f2937", "stroke-width": 1, "stroke-linecap": "round" }, lids);
      });
    }
  }

  function makeAgent(a) {
    var g = S("g", { "class": "ofc-agent" }, floorL);
    var fx = S("g", { "class": "ofc-fx" }, g);
    var person = A.person(a.character);
    fx.appendChild(person);
    addProps(person, a.character);
    var lbl = S("g", { "class": "ofc-lbl" }, labelL);
    var inner = S("g", { "class": "ofc-lbl-in" }, lbl);
    var zzz = S("g", { "class": "ofc-zzz" }, inner);
    T({ "class": "z1", x: 38, y: -76, "font-size": 13 }, zzz, "z");
    T({ "class": "z2", x: 49, y: -91, "font-size": 16 }, zzz, "z");
    T({ "class": "z3", x: 61, y: -107, "font-size": 20 }, zzz, "Z");
    inner.appendChild(A.tag(a.character.name));
    var dots = S("g", { "class": "ofc-dots" }, inner);
    S("rect", { x: -17, y: -146, width: 34, height: 17, rx: 8.5, fill: "#ffffff", stroke: "var(--waiting)", "stroke-width": 1.5 }, dots);
    [-8, 0, 8].forEach(function (dx, i) { S("circle", { "class": "d" + (i + 1), cx: dx, cy: -137.5, r: 2.4 }, dots); });
    var bub = S("g", {}, inner);
    return { id: a.id, g: g, fx: fx, person: person, lbl: lbl, bub: bub, a: a, key: null, first: true,
             pose: null, pos: null, dest: null, destBase: null, spotId: null, walking: false, token: 0,
             timers: [], legEnd: 0, facing: "" };
  }

  function claimDesk(a) {
    if (slotOf[a.id]) return slotOf[a.id];
    var d = null, i;
    if (a.role === "lead" && !leadDesk.owner) d = leadDesk;
    for (i = 0; !d && i < desks.length; i++) if (!desks[i].owner) d = desks[i];
    if (!d && !leadDesk.owner) d = leadDesk;
    if (!d) {                                  // more than five people: extra hot desks along the front
      var hx = [40, 520][hotDesks % 2] + Math.floor(hotDesks / 2) * 60;
      hotDesks++;
      d = drawWorkerDesk(hotDesks, hx, 552);
      d.seat.zone = "front"; d.review.zone = "front";
      desks.push(d);
    }
    d.owner = a.id;
    slotOf[a.id] = d;
    d.name.textContent = a.character.name.slice(0, 13);
    if (d.chip) d.chip.setAttribute("fill", a.character.shirt);
    if (d.plate) d.plate.style.display = "";
    if (d === leadDesk) glassDecal.textContent = a.character.name + (a.role === "lead" ? ", lead" : "");
    return d;
  }

  function keyOf(a) {
    var b = a.blockers && a.blockers[0];
    return [a.pose, a.target || "", a.task ? a.task.id : "", b ? b.task_id + b.reason : ""].join("|");
  }
  function near(p, q) { return p && q && Math.abs(p.x - q.x) < 2 && Math.abs(p.y - q.y) < 2; }

  // ------------------------------------------------------------------ routes + walking
  // Desks are reached through their own free column from the aisle; the office through its door.
  function exitPts(p) {
    if (p.zone === "desk") return [{ x: p.x, y: CFG.aisleY, zone: "front" }];
    if (p.zone === "office") return [{ x: CFG.doorX, y: CFG.officeY, zone: "office" }, { x: CFG.doorX, y: CFG.aisleY, zone: "front" }];
    return [];
  }
  function route(from, to) {
    var pts;
    if (from.zone === "office" && to.zone === "office") pts = [{ x: CFG.doorX, y: CFG.officeY, zone: "office" }];
    else pts = exitPts(from).concat(exitPts(to).reverse());
    pts.push(to);
    var out = [], last = from;
    pts.forEach(function (p) { if (!near(p, last)) { out.push(p); last = p; } });
    return out;
  }

  function walk(st, to, done) {
    var token = ++st.token, now = Date.now();
    var path = route(st.pos || to, to), from = st.pos || to;
    st.walking = true;
    st.g.classList.remove("face-left");
    if (st.legEnd > now) {              // an interrupted leg's timer will reset the facing: put it back
      later(function () { if (st.walking) st.person.setAttribute("transform", st.facing); }, st.legEnd - now + 40, st);
    }
    var i = 0;
    (function leg() {
      if (token !== st.token || !alive) return;
      if (i >= path.length) {
        st.walking = false;
        st.person.classList.remove("is-walking");
        setKey(st.g, to.y); sortFloor();
        if (done) done();
        return;
      }
      var p = path[i++], dist = Math.hypot(p.x - from.x, p.y - from.y);
      setKey(st.g, Math.max(from.y, p.y)); sortFloor();
      st.facing = p.x < from.x - 0.5 ? "scale(-1,1)" : "";
      st.legEnd = Date.now() + Math.min(4, Math.max(0.35, dist / 160)) * 1000;
      from = p; st.pos = p;
      var moving = A.moveTo(st.g, p.x, p.y, { walk: true, keepWalking: true });
      A.moveTo(st.lbl, p.x, p.y);
      moving.then(leg);
    })();
  }

  // ------------------------------------------------------------------ poses
  var POSE_CLASSES = ["st-sit", "st-sleep", "st-review", "st-blocked", "st-sip", "st-idle", "st-cheer", "face-left"];
  function clearPose(st) {
    POSE_CLASSES.forEach(function (c) { st.g.classList.remove(c); });
    st.person.classList.remove("is-typing");
  }
  function setLabel(st, extra) {
    var lead = st.a.role === "lead" ? " is-lead" : "";
    st.lbl.setAttribute("class", "ofc-lbl" + lead + (extra ? " " + extra : ""));
    st.lbl.style.setProperty("--pc", POSE_VAR[st.a.pose] || "#0002");
  }
  function setBubble(st, text) {
    st.bub.textContent = "";
    if (!text) return;
    var b = A.bubble(text);
    S("path", { d: "M-6,-128.6 L0,-122 L6,-128.6", fill: "#fff" }, b);
    S("rect", { x: -5, y: -130, width: 10, height: 2.4, fill: "#fff", stroke: "none" }, b);
    st.bub.appendChild(b);
  }
  function bubbleText(a) {
    var tid = a.task ? a.task.id : "";
    if (a.pose === "working") return tid || (a.role === "lead" && lastModel && lastModel.run.phase === "planning" ? "planning" : "working");
    if (a.pose === "reviewing") return "checking " + (tid || "work");
    if (a.pose === "blocked") {
      var b = a.blockers && a.blockers[0];
      return b ? (b.task_id + (b.reason ? ": " + b.reason : "")) : (tid ? tid + " blocked" : "blocked");
    }
    return "";
  }

  // What a person looks like once they are where they belong.
  function settle(st) {
    var a = st.a, g = st.g;
    clearPose(st);
    if (Date.now() < celebrateUntil) {             // arrived mid-party: join in, settle when it ends
      g.classList.add("st-cheer"); setLabel(st, ""); setBubble(st, "");
      return;
    }
    switch (a.pose) {
      case "working":
        g.classList.add("st-sit"); st.person.classList.add("is-typing");
        setLabel(st, "is-seated"); break;
      case "free":
        g.classList.add("st-sit"); g.classList.add("st-sleep");
        setLabel(st, "is-seated is-sleep"); break;
      case "blocked":
        g.classList.add("st-blocked"); setLabel(st, "is-blocked"); break;
      case "reviewing":
        g.classList.add("st-review");
        if (st.dest && st.dest.face < 0) g.classList.add("face-left");
        setLabel(st, ""); break;
      default:
        g.classList.add(st.dest && st.dest.act === "sip" ? "st-sip" : "st-idle");
        setLabel(st, "is-wait");
    }
    setBubble(st, bubbleText(a));
  }

  function releaseSpot(st) { if (st.spotId && taken[st.spotId] === st.id) delete taken[st.spotId]; st.spotId = null; }
  function pickSpot(st) {
    var cur = st.spotId, free = SPOTS.filter(function (s) { return s.id !== cur && !taken[s.id]; });
    if (!free.length) free = SPOTS.filter(function (s) { return s.id !== cur; });
    var s = free[Math.floor(Math.random() * free.length)];
    releaseSpot(st);
    taken[s.id] = st.id; st.spotId = s.id;
    return { x: s.x, y: s.y, zone: "front", act: s.act };
  }

  function destFor(st, a) {
    var own = slotOf[a.id];
    if (a.pose === "reviewing") {
      var d = a.target && a.target !== a.id ? slotOf[a.target] : null;
      var base = d ? d.review : REVIEW_TABLE, n = 0;
      Object.keys(people).forEach(function (id) { if (id !== st.id && people[id].destBase === base) n++; });
      st.destBase = base;
      return { x: base.x - base.face * 30 * n, y: base.y + 2 * n, zone: base.zone, face: base.face };
    }
    st.destBase = null;
    if (a.pose === "waiting") return pickSpot(st);
    return { x: own.seat.x, y: own.seat.y, zone: own.seat.zone };
  }

  function wander(st) {
    if (st.a.pose !== "waiting" || A.reducedMotion) return;
    var next = pickSpot(st);
    st.dest = next;
    clearPose(st); setBubble(st, ""); setLabel(st, "is-wait");
    walk(st, next, function () { settle(st); later(function () { wander(st); }, rand(CFG.wanderPause[0], CFG.wanderPause[1]), st); });
  }

  function transition(st, a, first) {
    clearOwn(st);
    if (a.pose !== "waiting") releaseSpot(st);
    var dest = destFor(st, a);
    st.dest = dest;
    if (first || A.reducedMotion) {
      st.token++; st.walking = false; st.person.classList.remove("is-walking");
      A.place(st.g, dest.x, dest.y); A.place(st.lbl, dest.x, dest.y);
      st.pos = dest; setKey(st.g, dest.y); sortFloor();
      settle(st);
      if (a.pose === "waiting" && !A.reducedMotion) later(function () { wander(st); }, rand(600, 1800), st);
      return;
    }
    if (!st.walking && near(st.pos, dest)) { settle(st); return; }   // same spot, new activity
    var seated = st.g.classList.contains("st-sit");
    clearPose(st); setBubble(st, "");
    setLabel(st, a.pose === "waiting" ? "is-wait" : "");
    later(function () {
      walk(st, dest, function () {
        settle(st);
        if (a.pose === "waiting") later(function () { wander(st); }, rand(CFG.wanderPause[0], CFG.wanderPause[1]), st);
      });
    }, seated ? CFG.standUpMs : 0, st);
  }

  // ------------------------------------------------------------------ desks, board, party
  function paintDesks(model) {
    var byId = {}, reviewed = {}, atTable = false;
    model.agents.forEach(function (a) {
      byId[a.id] = a;
      if (a.pose === "reviewing") {
        if (a.target && a.target !== a.id && slotOf[a.target]) reviewed[a.target] = true; else atTable = true;
      }
    });
    desks.concat([leadDesk]).forEach(function (d) {
      var a = d.owner ? byId[d.owner] : null, mode = "off", led = "#64748b";
      if (a) {
        mode = reviewed[a.id] ? "diff" : a.pose === "working" ? "code" : a.pose === "blocked" ? "alert"
             : a.pose === "free" ? "sleep" : "lock";
        led = reviewed[a.id] ? POSE_VAR.reviewing : POSE_VAR[a.pose];
      }
      setScreen(d.scr, mode);
      d.led.setAttribute("fill", led);
    });
    setScreen(tableScreen, atTable ? "diff" : "lock");
  }

  function confetti(colors) {
    var g = S("g", { "class": "ofc-confetti", "clip-path": "url(#ofc-stage)" }, topL);
    for (var i = 0; i < CFG.confetti; i++) {
      var style = "--x:" + Math.round(rand(20, 980)) + "px;--dx:" + Math.round(rand(-70, 70)) + "px;--r:" +
                  Math.round(rand(300, 900)) * (i % 2 ? 1 : -1) + "deg;--d:" + rand(2.4, 4.2).toFixed(2) + "s;--delay:" +
                  rand(0, 0.9).toFixed(2) + "s";
      S("rect", { "class": "ofc-conf", x: -3, y: -5, width: i % 3 ? 6 : 8, height: i % 3 ? 10 : 5, rx: 1,
                  fill: colors[i % colors.length], style: style }, g);
    }
    later(function () { if (g.parentNode) g.parentNode.removeChild(g); }, 5600);
  }

  function celebrate(model) {
    var colors = model.agents.map(function (a) { return a.character.shirt; });
    drawBalloons(colors.length ? colors : ["#4c7fe6", "#e0a23b", "#2f9e6f"]);
    if (A.reducedMotion) return;
    confetti(colors.concat(["#e0a23b", "#2f9e6f", "#8a63d2", "#d4614f"]));
    celebrateUntil = Date.now() + CFG.cheerMs;
    Object.keys(people).forEach(function (id) {
      var st = people[id];
      if (st.first || st.walking) return;
      clearOwn(st); clearPose(st); setBubble(st, "");
      st.g.classList.add("st-cheer"); setLabel(st, "");
    });
    later(function () {                          // party over: everyone settles into their real state
      Object.keys(people).forEach(function (id) { people[id].g.classList.remove("st-cheer"); people[id].key = null; });
      if (lastModel) applyAgents(lastModel);
    }, CFG.cheerMs + 50);
  }

  function tick() {
    if (!clockEls) return;
    var d = new Date(), s = d.getSeconds(), m = d.getMinutes() + s / 60, h = (d.getHours() % 12) + m / 60;
    clockEls.h.setAttribute("transform", "rotate(" + (h * 30) + ")");
    clockEls.m.setAttribute("transform", "rotate(" + (m * 6) + ")");
    clockEls.s.setAttribute("transform", "rotate(" + (s * 6) + ")");
  }

  var lastModel = null;
  function applyAgents(model) {
    var celebrating = Date.now() < celebrateUntil;
    model.agents.forEach(function (a) {
      var st = people[a.id];
      st.a = a;
      var key = keyOf(a);
      if (!st.first && (st.key === key || celebrating)) return;
      var first = st.first;
      st.first = false; st.key = key;
      transition(st, a, first);
    });
  }

  // ------------------------------------------------------------------ scene
  OmniOffice.registerScene({
    id: "office", label: "Office", icon: "🏢",
    mount: function (svg, api) {
      A = api; alive = true;
      people = {}; desks = []; slotOf = {}; taken = {}; timers = []; hotDesks = 0;
      wasComplete = false; celebrateUntil = 0; lastModel = null;
      styleEl = api.css(CSS);
      root = S("g", { "class": "ofc" }, svg);
      defsEl = S("defs", {}, root);
      bgL = S("g", {}, root);
      drawRoom(defsEl);
      drawBoard();
      floorL = S("g", {}, root);
      labelL = S("g", {}, root);
      topL = S("g", { "pointer-events": "none" }, root);
      S("rect", { width: 1000, height: 560 }, S("clipPath", { id: "ofc-stage" }, defsEl));
      CFG.workerX.forEach(function (x, i) { desks.push(drawWorkerDesk(i, x, CFG.seatY)); });
      leadDesk = drawLeadDesk();
      glassDecal = drawGlassFront("");
      drawProps();
      sortFloor();
      tick();
      tickTimer = setInterval(tick, 1000);
    },
    update: function (model, api) {
      A = api;
      lastModel = model;
      var run = model.run;
      renderBoard(run);
      model.agents.forEach(claimDesk);
      var ids = {};
      model.agents.forEach(function (a) { ids[a.id] = true; if (!people[a.id]) people[a.id] = makeAgent(a); });
      Object.keys(people).forEach(function (id) {   // someone left the team: take them off the floor
        if (ids[id]) return;
        var st = people[id];
        clearOwn(st); releaseSpot(st); st.token++;
        [st.g, st.lbl].forEach(function (n) { if (n.parentNode) n.parentNode.removeChild(n); });
        delete people[id];
      });
      root.classList.toggle("is-complete", !!run.complete);
      if (run.complete && !wasComplete) celebrate(model);
      wasComplete = !!run.complete;
      applyAgents(model);
      paintDesks(model);
    },
    unmount: function () {
      alive = false;
      timers.forEach(clearTimeout); timers = [];
      if (tickTimer) clearInterval(tickTimer);
      tickTimer = null;
      if (styleEl && styleEl.parentNode) styleEl.parentNode.removeChild(styleEl);
      styleEl = null; people = {}; desks = []; slotOf = {}; taken = {}; clockEls = null; board = null;
    }
  });
})();
