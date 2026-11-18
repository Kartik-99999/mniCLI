/* OmniCLI Office - shared runtime.
 *
 * Scene contract (scene_*.js files call OmniOffice.registerScene):
 *   OmniOffice.registerScene({
 *     id: "office", label: "Office", icon: "🏢",
 *     mount(svg, api)      // build the scene once inside `svg` (viewBox 0 0 1000 560)
 *     update(model, api)   // called on every refresh (~1 s); keep elements keyed by agent id,
 *                          // animate only what changed (pose / target / task)
 *     unmount()            // stop timers / listeners (the svg is cleared by the runtime)
 *   });
 *
 * model = { run: {name, goal, phase, alive, complete, counts:{done,failed,running,pending,total}},
 *           agents: [ { id, role: "lead"|"worker",
 *                       pose: "working"|"reviewing"|"waiting"|"free"|"blocked",
 *                       status: raw run status ("working","planning","checking","reviewing","queued",...),
 *                       target: <agent id whose work is being reviewed> | null,
 *                       task: {id, title} | null, latest: "", blockers: [{task_id, reason}],
 *                       done: <n>, total: <n>, completed: [{id,title,summary}], character: {...} } ] }
 *
 * api = { svg(tag, attrs, parent), person(character) -> <g class="person">, tag(text) -> <g>,
 *         bubble(text) -> <g>, moveTo(g, x, y, {walk}), css(text), reducedMotion, character(id) }
 *
 * Characters are plain data (OmniOffice.CHARACTERS) so they can be chosen by the user later.
 */
(function () {
  "use strict";
  var NS = "http://www.w3.org/2000/svg";
  var OO = window.OmniOffice = { scenes: {}, order: [] };

  OO.reducedMotion = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);

  // ---------------------------------------------------------------- characters (data only)
  OO.CHARACTERS = {
    claude: { name: "Claude", skin: "#e9b997", hair: "#5b3a26", hairStyle: "short", shirt: "#d97757", pants: "#3a3f4b", accessory: "glasses", number: 1 },
    codex:  { name: "Codex",  skin: "#c58b5c", hair: "#1e1e1e", hairStyle: "spiky", shirt: "#10a37f", pants: "#2d3748", accessory: "headphones", number: 7 },
    agy:    { name: "Antigravity", skin: "#f1c8a3", hair: "#7a4a2a", hairStyle: "bun", shirt: "#4c7fe6", pants: "#374151", accessory: "none", number: 10 },
    muse:   { name: "Muse",   skin: "#8b5a3c", hair: "#24170f", hairStyle: "long", shirt: "#8b5cf6", pants: "#1f2937", accessory: "none", number: 18 },
    human:  { name: "You",    skin: "#d9a67e", hair: "#3c2a1e", hairStyle: "cap", shirt: "#f59e0b", pants: "#334155", accessory: "none", number: 99 },
    default:{ name: "",       skin: "#d4a682", hair: "#4a3a30", hairStyle: "short", shirt: "#94a3b8", pants: "#3f4652", accessory: "none", number: 0 }
  };
  OO.character = function (id) {
    var base = OO.CHARACTERS["default"], own = OO.CHARACTERS[id] || {};
    var c = {};
    Object.keys(base).forEach(function (k) { c[k] = own[k] !== undefined ? own[k] : base[k]; });
    if (!c.name) c.name = id;
    return c;
  };

  // ---------------------------------------------------------------- svg helpers
  OO.svg = function (tag, attrs, parent) {
    var n = document.createElementNS(NS, tag);
    Object.keys(attrs || {}).forEach(function (k) { n.setAttribute(k, attrs[k]); });
    if (parent) parent.appendChild(n);
    return n;
  };
  OO.css = function (text) {
    var s = document.createElement("style");
    s.textContent = text;
    document.head.appendChild(s);
    return s;
  };

  /* A person standing on (0,0), about 92 units tall, facing the viewer.
     Groups: .p-body > .p-leg-l .p-leg-r .p-torso .p-arm-l .p-arm-r .p-head, plus .p-prop for scene props.
     Limbs pivot at their top edge (shoulder / hip), the head at its bottom (neck). */
  OO.person = function (c) {
    var S = OO.svg, g = S("g", { "class": "person", "data-char": c.name });
    var body = S("g", { "class": "p-body" }, g);
    S("ellipse", { cx: 0, cy: 1, rx: 17, ry: 4, fill: "#00000022", "class": "p-shadow" }, g);
    S("rect", { "class": "p-limb p-leg-l", x: -9, y: -34, width: 8, height: 33, rx: 4, fill: c.pants }, body);
    S("rect", { "class": "p-limb p-leg-r", x: 1, y: -34, width: 8, height: 33, rx: 4, fill: c.pants }, body);
    S("rect", { "class": "p-torso", x: -13, y: -62, width: 26, height: 32, rx: 9, fill: c.shirt }, body);
    if (c.number) {
      var num = S("text", { x: 0, y: -41, "text-anchor": "middle", "font-size": 11, "font-weight": 700,
        fill: "#ffffffd0", "class": "p-number" }, body);
      num.textContent = String(c.number);
    }
    S("rect", { "class": "p-limb p-arm-l", x: -19, y: -60, width: 7, height: 27, rx: 3.5, fill: c.shirt }, body);
    S("rect", { "class": "p-limb p-arm-r", x: 12, y: -60, width: 7, height: 27, rx: 3.5, fill: c.shirt }, body);
    var head = S("g", { "class": "p-head" }, body);
    S("rect", { x: -3.5, y: -67, width: 7, height: 7, fill: c.skin }, head);
    S("circle", { cx: 0, cy: -77, r: 12, fill: c.skin }, head);
    var hair = c.hair;
    switch (c.hairStyle) {
      case "long":  S("path", { d: "M-13,-78 a13,13 0 0 1 26,0 v14 h-5 v-12 h-16 v12 h-5z", fill: hair }, head); break;
      case "bun":   S("path", { d: "M-12,-80 a12,12 0 0 1 24,0 v-1 h-24z", fill: hair }, head);
                    S("circle", { cx: 0, cy: -92, r: 5.5, fill: hair }, head); break;
      case "spiky": S("path", { d: "M-12,-80 l3,-10 l4,6 l5,-9 l4,8 l5,-7 l3,12z", fill: hair }, head); break;
      case "cap":   S("path", { d: "M-13,-80 a13,11 0 0 1 26,0z", fill: c.shirt }, head);
                    S("rect", { x: 6, y: -82, width: 12, height: 3.5, rx: 1.5, fill: c.shirt }, head); break;
      case "bald":  break;
      default:      S("path", { d: "M-12,-79 a12,12 0 0 1 24,0 q-12,-5 -24,0z", fill: hair }, head);
    }
    if (c.accessory === "glasses") {
      S("circle", { cx: -4.5, cy: -77, r: 3.4, fill: "none", stroke: "#1f2937", "stroke-width": 1.3 }, head);
      S("circle", { cx: 4.5, cy: -77, r: 3.4, fill: "none", stroke: "#1f2937", "stroke-width": 1.3 }, head);
      S("line", { x1: -1.1, y1: -77, x2: 1.1, y2: -77, stroke: "#1f2937", "stroke-width": 1.3 }, head);
    } else {
      S("circle", { cx: -4, cy: -77, r: 1.3, fill: "#1f2937" }, head);
      S("circle", { cx: 4, cy: -77, r: 1.3, fill: "#1f2937" }, head);
    }
    if (c.accessory === "headphones") {
      S("path", { d: "M-13,-78 a13,13 0 0 1 26,0", fill: "none", stroke: "#111827", "stroke-width": 2.5 }, head);
      S("rect", { x: -15, y: -81, width: 5, height: 9, rx: 2, fill: "#111827" }, head);
      S("rect", { x: 10, y: -81, width: 5, height: 9, rx: 2, fill: "#111827" }, head);
    }
    S("path", { d: "M-3.5,-71 q3.5,2.5 7,0", fill: "none", stroke: "#7c2d12", "stroke-width": 1.2, "stroke-linecap": "round" }, head);
    S("g", { "class": "p-prop" }, g);
    return g;
  };

  /* Name tag centred above a person (y is the tag's baseline position, relative to the feet). */
  OO.tag = function (text) {
    var g = OO.svg("g", { "class": "oo-tag" });
    var w = Math.max(44, text.length * 7.6 + 16);
    OO.svg("rect", { x: -w / 2, y: -122, width: w, height: 20, rx: 10 }, g);
    var t = OO.svg("text", { x: 0, y: -108, "text-anchor": "middle" }, g);
    t.textContent = text;
    return g;
  };
  OO.bubble = function (text) {
    var g = OO.svg("g", { "class": "oo-bubble" });
    var s = String(text || "").slice(0, 28), w = Math.max(40, s.length * 6.4 + 14);
    OO.svg("rect", { x: -w / 2, y: -150, width: w, height: 22, rx: 8 }, g);
    var t = OO.svg("text", { x: 0, y: -135, "text-anchor": "middle" }, g);
    t.textContent = s;
    return g;
  };

  /* Move a group to (x, y). With {walk:true} the person inside gets the walking cycle while moving.
     Speed is constant (about 160 units/s) so long walks take longer. Returns a Promise. */
  OO.moveTo = function (g, x, y, opts) {
    opts = opts || {};
    var hasPos = g.dataset.x !== undefined;
    var fromX = hasPos ? +g.dataset.x : x, fromY = hasPos ? +g.dataset.y : y;
    var dist = Math.hypot(x - fromX, y - fromY);
    g.dataset.x = x; g.dataset.y = y;
    if (!hasPos || OO.reducedMotion || dist < 1) {
      g.style.transition = "none";
      g.style.transform = "translate(" + x + "px," + y + "px)";
      return Promise.resolve();
    }
    var secs = Math.min(4, Math.max(0.35, dist / 160));
    var person = g.querySelector(".person");
    if (g._ooWalkTimer) { clearTimeout(g._ooWalkTimer); g._ooWalkTimer = null; }
    if (opts.walk && person) person.classList.add("is-walking");
    if (person) person.setAttribute("transform", x < fromX ? "scale(-1,1)" : "");
    g.style.transition = "transform " + secs + "s linear";
    g.style.transform = "translate(" + x + "px," + y + "px)";
    return new Promise(function (resolve) {
      g._ooWalkTimer = setTimeout(function () {
        g._ooWalkTimer = null;
        if (opts.walk && person && !opts.keepWalking) person.classList.remove("is-walking");
        if (person) person.removeAttribute("transform");
        resolve();
      }, secs * 1000);
    });
  };
  /* Place without animation (first render). */
  OO.place = function (g, x, y) {
    g.dataset.x = x; g.dataset.y = y;
    g.style.transition = "none";
    g.style.transform = "translate(" + x + "px," + y + "px)";
  };

  // ---------------------------------------------------------------- data -> model
  var POSE_LABEL = { working: "working", reviewing: "checking work", waiting: "waiting", free: "free", blocked: "blocked" };
  var POSE_COLOR = { working: "var(--working)", reviewing: "var(--reviewing)", waiting: "var(--waiting)",
                     free: "var(--free)", blocked: "var(--blocked)" };
  OO.POSE_LABEL = POSE_LABEL;

  function poseFor(a, run) {
    if (!run.alive && run.phase !== "running" && run.phase !== "planning") return "free";
    switch (a.status) {
      case "working": case "planning": case "checking": return "working";
      case "reviewing": return "reviewing";
      case "queued": return "waiting";
      case "blocked": case "interrupted": return "blocked";
      default: return "free";
    }
  }

  OO.deriveModel = function (data) {
    var counts = data.counts || {};
    var run = { name: data.project_name || data.dir || "", goal: data.goal || "", phase: data.phase || "",
                alive: !!data.alive, complete: !!(data.final && data.final.complete) || data.phase === "complete",
                counts: { done: counts.done || 0, failed: counts.failed || 0, running: counts.running || 0,
                          pending: counts.pending || 0, total: counts.total || 0 } };
    var agents = (data.agents || []).map(function (a) {
      var p = a.progress || {}, rv = a.reviewing_task || null;
      var task = (a.current_tasks && a.current_tasks[0]) || null;
      if (!task && rv) task = { id: rv.task_id, title: rv.title || "" };
      return {
        id: a.id, role: a.role || "worker", pose: poseFor(a, run), status: a.status || "",
        target: rv && rv.builder && rv.builder !== a.id ? rv.builder : null,
        task: task, latest: a.latest_output || "", blockers: a.blockers || [],
        done: p.done || 0, total: p.total || 0, completed: a.completed_work || [],
        character: OO.character(a.id)
      };
    });
    return { run: run, agents: agents };
  };

  // ---------------------------------------------------------------- demo (#demo): no run needed
  var DEMO = [
    { phase: "planning", agents: { claude: ["working"], codex: ["idle"], agy: ["idle"], muse: ["idle"] }, note: "Lead plans" },
    { phase: "running", agents: { claude: ["idle"], codex: ["working", "t1", "Build the API"], agy: ["working", "t2", "Build the UI"], muse: ["queued"] } },
    { phase: "running", agents: { claude: ["reviewing", "t1", "", "codex"], codex: ["queued"], agy: ["working", "t2", "Build the UI"], muse: ["queued"] } },
    { phase: "running", agents: { claude: ["reviewing", "t2", "", "agy"], codex: ["working", "t3", "Write tests"], agy: ["queued"], muse: ["working", "t4", "Write README"] } },
    { phase: "running", agents: { claude: ["idle"], codex: ["working", "t3", "Write tests"], agy: ["idle"], muse: ["blocked", "t4", "Waiting on t3"] } },
    { phase: "complete", agents: { claude: ["idle"], codex: ["idle"], agy: ["idle"], muse: ["idle"] } }
  ];
  function demoSnapshot(step) {
    var s = DEMO[step % DEMO.length], agents = [];
    Object.keys(s.agents).forEach(function (id) {
      var v = s.agents[id];
      agents.push({ id: id, role: id === "claude" ? "lead" : "worker", status: v[0],
        current_tasks: v[0] === "working" && v[1] ? [{ id: v[1], title: v[2] }] : [],
        reviewing_task: v[0] === "reviewing" ? { task_id: v[1], builder: v[3] } : null,
        blockers: v[0] === "blocked" ? [{ task_id: v[1], reason: v[2] }] : [],
        latest_output: v[0] === "working" ? "editing files..." : "",
        progress: { done: Math.min(step % DEMO.length, 4), total: 4 },
        completed_work: [] });
    });
    return { project_name: "Demo team", goal: "Demo mode: cycles through every state (no run needed).",
             phase: s.phase, alive: s.phase !== "complete",
             counts: { done: Math.min(step % DEMO.length, 4), total: 4, failed: 0 },
             final: s.phase === "complete" ? { complete: true } : null, agents: agents };
  }

  // ---------------------------------------------------------------- runtime
  var stage, svgRoot, current = null, lastModel = null, demo = /(^|#|&)demo(=|&|$)/.test(location.hash);

  OO.registerScene = function (scene) {
    OO.scenes[scene.id] = scene;
    OO.order.push(scene.id);
  };

  function api() {
    return { svg: OO.svg, person: OO.person, tag: OO.tag, bubble: OO.bubble, moveTo: OO.moveTo, place: OO.place,
             css: OO.css, character: OO.character, reducedMotion: OO.reducedMotion, poseLabel: POSE_LABEL };
  }

  function mountScene(id) {
    if (current && current.unmount) { try { current.unmount(); } catch (e) { console.error(e); } }
    stage.textContent = "";
    svgRoot = OO.svg("svg", { viewBox: "0 0 1000 560", preserveAspectRatio: "xMidYMid meet", role: "img",
                              "aria-label": "Animated view of the AI team" }, stage);
    current = OO.scenes[id] || OO.scenes[OO.order[0]];
    try { current.mount(svgRoot, api()); } catch (e) { console.error(e); }
    if (lastModel) render(lastModel);
  }

  function text(id, value) { var n = document.getElementById(id); if (n) n.textContent = value; }

  function renderRoster(model) {
    var ul = document.getElementById("roster");
    ul.textContent = "";
    if (!model.agents.length) { var e = document.createElement("li"); e.className = "empty"; e.textContent = "No agents yet."; ul.appendChild(e); return; }
    model.agents.forEach(function (a) {
      var li = document.createElement("li");
      li.style.setProperty("--c", POSE_COLOR[a.pose]);
      var who = document.createElement("div"); who.className = "who";
      var nm = document.createElement("span"); nm.textContent = a.character.name + (a.role === "lead" ? " (lead)" : "");
      var ps = document.createElement("span"); ps.className = "pose"; ps.textContent = POSE_LABEL[a.pose];
      who.append(nm, ps); li.appendChild(who);
      function line(label, value) {
        if (!value) return;
        var d = document.createElement("div"); d.className = "meta"; d.textContent = label + " " + value; li.appendChild(d);
      }
      if (a.pose === "reviewing") line("Checking:", (a.task ? a.task.id : "") + (a.target ? " by " + OO.character(a.target).name : ""));
      else if (a.task) line("Task:", a.task.id + " " + (a.task.title || ""));
      a.blockers.slice(0, 2).forEach(function (b) { line("Blocked:", b.task_id + " " + b.reason); });
      line("Latest:", a.latest);
      line("Progress:", a.done + "/" + a.total + " tasks done");
      if (a.completed.length) {
        var det = document.createElement("details"), sum = document.createElement("summary");
        sum.textContent = "Completed work (" + a.completed.length + ")";
        det.appendChild(sum);
        a.completed.forEach(function (t) { var d = document.createElement("div"); d.textContent = t.id + " " + t.title; det.appendChild(d); });
        li.appendChild(det);
      }
      ul.appendChild(li);
    });
  }

  function render(model) {
    lastModel = model;
    text("project", model.run.name + " · " + model.run.phase);
    text("goal", model.run.goal);
    text("phase", model.run.alive ? "● RUNNING" : model.run.phase === "idle" ? "● READY · waiting for a task"
                                  : "● " + String(model.run.phase || "stopped").toUpperCase());
    var c = model.run.counts;
    text("progress", c.done + "/" + c.total + " tasks done" + (c.failed ? " · " + c.failed + " failed" : ""));
    renderRoster(model);
    if (current) { try { current.update(model, api()); } catch (e) { console.error(e); } }
  }

  var demoStep = 0;
  function poll() {
    if (demo) {
      render(OO.deriveModel(demoSnapshot(demoStep++)));
      text("refresh", "Demo · step " + (((demoStep - 1) % DEMO.length) + 1) + "/" + DEMO.length);
      return;
    }
    fetch("/snapshot", { cache: "no-store" }).then(function (r) {
      if (!r.ok) throw new Error("Run state unavailable");
      return r.json();
    }).then(function (data) {
      render(OO.deriveModel(data));
      text("refresh", "Updated " + new Date().toLocaleTimeString());
    }).catch(function () { text("refresh", "Connection issue · retrying"); });
  }

  OO.start = function () {
    stage = document.getElementById("stage");
    var pick = document.getElementById("scene");
    OO.order.forEach(function (id) {
      var o = document.createElement("option");
      o.value = id; o.textContent = (OO.scenes[id].icon ? OO.scenes[id].icon + " " : "") + OO.scenes[id].label;
      pick.appendChild(o);
    });
    var saved = null;
    try { saved = localStorage.getItem("omnicliOfficeScene"); } catch (e) { /* storage blocked */ }
    var first = saved && OO.scenes[saved] ? saved : OO.order[0];
    pick.value = first;
    pick.addEventListener("change", function () {
      try { localStorage.setItem("omnicliOfficeScene", pick.value); } catch (e) { /* storage blocked */ }
      mountScene(pick.value);
    });
    mountScene(first);
    poll();
    setInterval(poll, demo ? 6000 : 1000);
  };
})();
