/* Geyser 3D terrains: the WebGL half of surface.js. Runs in a Worker on an OffscreenCanvas, so creating the
   context (which can wait on the browser's GPU process for a few hundred ms on a cold start) and every draw stay
   off the page's main thread. Browsers without OffscreenCanvas load this same file as a plain script and draw on
   the main thread instead (self.GeyserGL). */
(() => {
  "use strict";

  function createRenderer(canvas) {
    const gl = canvas.getContext("webgl", { antialias: true, alpha: true });
    if (!gl) throw new Error("no WebGL");
    const shader = (type, src) => {
      const s = gl.createShader(type);
      gl.shaderSource(s, src);
      gl.compileShader(s);
      return s;
    };
    const prog = gl.createProgram();
    gl.attachShader(prog, shader(gl.VERTEX_SHADER, "attribute vec3 p;attribute vec3 c;uniform mat4 m;varying vec3 v;void main(){gl_Position=m*vec4(p,1.);v=c;}"));
    gl.attachShader(prog, shader(gl.FRAGMENT_SHADER, "precision mediump float;varying vec3 v;void main(){gl_FragColor=vec4(v,1.);}"));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error("WebGL program failed");
    const aP = gl.getAttribLocation(prog, "p"), aC = gl.getAttribLocation(prog, "c"), uM = gl.getUniformLocation(prog, "m");
    const buffer = (arr) => {
      const b = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, b);
      gl.bufferData(gl.ARRAY_BUFFER, arr, gl.STATIC_DRAW);
      return b;
    };
    let meshes = [];
    return {
      // geometry: {pos, col, lines, lineCol} as Float32Arrays (xyz and rgb per vertex)
      load(geo) {
        meshes = [
          { p: buffer(geo.lines), c: buffer(geo.lineCol), mode: gl.LINES, n: geo.lines.length / 3 },
          { p: buffer(geo.pos), c: buffer(geo.col), mode: gl.TRIANGLES, n: geo.pos.length / 3 },
        ];
      },
      // view: {matrix (16 numbers, column-major), width, height} in device pixels
      draw(view) {
        if (canvas.width !== view.width || canvas.height !== view.height) {
          canvas.width = view.width;
          canvas.height = view.height;
        }
        gl.viewport(0, 0, canvas.width, canvas.height);
        gl.clearColor(0, 0, 0, 0);
        gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
        gl.enable(gl.DEPTH_TEST);
        gl.useProgram(prog);
        gl.uniformMatrix4fv(uM, false, new Float32Array(view.matrix));
        for (const m of meshes) {
          gl.bindBuffer(gl.ARRAY_BUFFER, m.p); gl.enableVertexAttribArray(aP); gl.vertexAttribPointer(aP, 3, gl.FLOAT, false, 0, 0);
          gl.bindBuffer(gl.ARRAY_BUFFER, m.c); gl.enableVertexAttribArray(aC); gl.vertexAttribPointer(aC, 3, gl.FLOAT, false, 0, 0);
          gl.drawArrays(m.mode, 0, m.n);
        }
      },
    };
  }

  const inWorker = typeof WorkerGlobalScope !== "undefined" && self instanceof WorkerGlobalScope;
  if (!inWorker) {
    self.GeyserGL = createRenderer;
    return;
  }
  let renderer = null, pending = null;
  self.onmessage = (e) => {
    const msg = e.data;
    if (msg.type === "init") {
      try {
        renderer = createRenderer(msg.canvas);
        renderer.load(msg.geometry);
        self.postMessage({ type: "ready" });
      } catch (err) {
        self.postMessage({ type: "fail", reason: String(err) });
      }
    } else if (msg.type === "draw" && renderer) {
      // Only the latest view matters: coalesce bursts of drags into one draw per animation frame.
      const first = !pending;
      pending = msg.view;
      if (first) {
        (self.requestAnimationFrame || ((f) => setTimeout(f, 16)))(() => {
          renderer.draw(pending);
          pending = null;
        });
      }
    }
  };
})();
