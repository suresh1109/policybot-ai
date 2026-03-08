/**
 * PolicyBot 3D Insurance Visualizer v3
 * Full immersive 3D animation using Three.js r128
 * Features:
 *  - Animated central icosahedron with emissive glow + inner crystals
 *  - Orbiting plan rings with animated nodes
 *  - Floating particle field 280 particles
 *  - Pulsing energy beams from core to plan nodes
 *  - Camera gentle auto-orbit
 *  - Insurance-type color themes
 *  - Plan node highlighting on selection
 */

(function() {
  'use strict';

  let scene, camera, renderer, clock;
  let animFrame = null;
  let container = null;
  let coreGroup = null;
  let ringGroup = null;
  let particles = null;
  let beamLines = [];
  let planNodes = [];

  const THEMES = {
    'Health Insurance':      { c1: 0x10b981, c2: 0x34d399, c3: 0x6ee7b7, bg: 0x011a0e, glow: 0.9  },
    'Term / Life Insurance': { c1: 0x3b82f6, c2: 0x60a5fa, c3: 0x93c5fd, bg: 0x060d1f, glow: 0.85 },
    'Vehicle Insurance':     { c1: 0xf59e0b, c2: 0xfbbf24, c3: 0xfde68a, bg: 0x1a0f00, glow: 1.0  },
    'Travel Insurance':      { c1: 0x06b6d4, c2: 0x22d3ee, c3: 0x67e8f9, bg: 0x011018, glow: 0.9  },
    'Property Insurance':    { c1: 0xef4444, c2: 0xf87171, c3: 0xfca5a5, bg: 0x1a0303, glow: 0.85 },
    'Accident Insurance':    { c1: 0xa855f7, c2: 0xc084fc, c3: 0xe9d5ff, bg: 0x10031f, glow: 1.0  },
  };
  const DEF = { c1: 0x5a72ff, c2: 0x818cf8, c3: 0xc7d2fe, bg: 0x06081a, glow: 0.9 };

  window.PolicyBot3D = {

    init(containerId, insuranceType, planNames) {
      this.destroy();
      container = document.getElementById(containerId);
      if (!container || typeof THREE === 'undefined') return;

      const W = container.clientWidth  || 300;
      const H = container.clientHeight || 260;
      const T = THEMES[insuranceType] || DEF;

      // Scene
      scene = new THREE.Scene();
      scene.fog = new THREE.FogExp2(T.bg, 0.10);

      // Camera
      camera = new THREE.PerspectiveCamera(52, W / H, 0.05, 200);
      camera.position.set(0, 1.0, 7.8);
      camera.lookAt(0, 0, 0);

      // Renderer
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
      renderer.setSize(W, H);
      renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
      renderer.setClearColor(T.bg, 0.96);
      container.appendChild(renderer.domElement);
      clock = new THREE.Clock();

      // Lights
      scene.add(new THREE.AmbientLight(0xffffff, 0.28));
      const p1 = new THREE.PointLight(T.c2, 3.5, 14);
      p1.position.set(0, 3, 3); scene.add(p1);
      const p2 = new THREE.PointLight(T.c1, 2.0, 10);
      p2.position.set(0, -2, -2); scene.add(p2);
      const spot = new THREE.SpotLight(T.c3, 5, 22, Math.PI / 5, 0.4);
      spot.position.set(3, 5, 4); scene.add(spot);

      // ── CORE GROUP ──
      coreGroup = new THREE.Group();
      scene.add(coreGroup);

      // Outer glow halo
      const haloGeo = new THREE.SphereGeometry(1.7, 32, 32);
      const haloMat = new THREE.MeshBasicMaterial({ color: T.c1, transparent: true, opacity: 0.06, side: THREE.BackSide });
      coreGroup.add(new THREE.Mesh(haloGeo, haloMat));

      // Main body — icosahedron
      const coreGeo = new THREE.IcosahedronGeometry(1.08, 1);
      const coreMat = new THREE.MeshPhongMaterial({
        color: T.c1, emissive: T.c1, emissiveIntensity: T.glow * 0.55,
        transparent: true, opacity: 0.84, shininess: 160, specular: T.c3,
      });
      const coreMesh = new THREE.Mesh(coreGeo, coreMat);
      coreGroup.add(coreMesh);
      coreGroup.userData.coreMesh = coreMesh;

      // Wireframe shell
      const wireMat = new THREE.MeshBasicMaterial({ color: T.c3, wireframe: true, transparent: true, opacity: 0.18 });
      const wireMesh = new THREE.Mesh(coreGeo, wireMat);
      wireMesh.scale.setScalar(1.055);
      coreGroup.add(wireMesh);
      coreGroup.userData.wireMesh = wireMesh;

      // Mid octahedron
      const midGeo = new THREE.OctahedronGeometry(0.68, 0);
      const midMat = new THREE.MeshPhongMaterial({
        color: T.c2, emissive: T.c2, emissiveIntensity: 1.0,
        transparent: true, opacity: 0.62, shininess: 240,
      });
      const midMesh = new THREE.Mesh(midGeo, midMat);
      coreGroup.add(midMesh);
      coreGroup.userData.midMesh = midMesh;

      // Inner crystal tetrahedron
      const xtalGeo = new THREE.TetrahedronGeometry(0.38, 0);
      const xtalMat = new THREE.MeshPhongMaterial({
        color: T.c3, emissive: T.c3, emissiveIntensity: 1.4,
        transparent: true, opacity: 0.92, shininess: 300,
      });
      const xtalMesh = new THREE.Mesh(xtalGeo, xtalMat);
      coreGroup.add(xtalMesh);
      coreGroup.userData.xtalMesh = xtalMesh;

      // Equatorial ring disc
      const discGeo = new THREE.TorusGeometry(1.55, 0.04, 8, 96);
      const discMat = new THREE.MeshBasicMaterial({ color: T.c2, transparent: true, opacity: 0.4 });
      const disc = new THREE.Mesh(discGeo, discMat);
      disc.rotation.x = -Math.PI / 2.3;
      scene.add(disc);
      scene.userData.disc = disc;

      // ── ORBIT RINGS + PLAN NODES ──
      ringGroup = new THREE.Group();
      scene.add(ringGroup);

      const plans    = planNames || [];
      const nPlans   = Math.min(plans.length, 3);
      const rRadii   = [2.7, 3.6, 4.3];
      const rTilts   = [0, Math.PI / 5.5, -Math.PI / 7];
      const rSpeeds  = [0.30, -0.20, 0.24];

      planNodes = [];

      for (let ri = 0; ri < nPlans; ri++) {
        const rad = rRadii[ri];
        const tilt = rTilts[ri];
        const speed = rSpeeds[ri];

        // Torus ring
        const tGeo = new THREE.TorusGeometry(rad, 0.022, 6, 128);
        const tMat = new THREE.MeshBasicMaterial({ color: T.c2, transparent: true, opacity: 0.30 });
        const ring = new THREE.Mesh(tGeo, tMat);
        ring.rotation.x = tilt;
        ring.userData.speed = speed;
        ring.userData.isRing = true;
        ringGroup.add(ring);

        // Node sphere at initial position
        const a0 = (ri / nPlans) * Math.PI * 2;
        const nx  = rad * Math.cos(a0);
        const ny  = rad * Math.sin(a0) * Math.sin(tilt);
        const nz  = rad * Math.sin(a0) * Math.cos(tilt);

        const nGeo = new THREE.SphereGeometry(0.20, 20, 20);
        const nMat = new THREE.MeshPhongMaterial({
          color: T.c2, emissive: T.c2, emissiveIntensity: 0.85,
          transparent: true, opacity: 0.92, shininess: 200,
        });
        const node = new THREE.Mesh(nGeo, nMat);
        node.position.set(nx, ny, nz);
        node.userData.rad   = rad;
        node.userData.tilt  = tilt;
        node.userData.speed = speed;
        node.userData.angle = a0;
        node.userData.idx   = ri;
        ringGroup.add(node);
        planNodes.push(node);

        // Cross spikes on node
        for (let s = 0; s < 4; s++) {
          const sG = new THREE.CylinderGeometry(0.007, 0.007, 0.32, 4);
          const sM = new THREE.MeshBasicMaterial({ color: T.c3, transparent: true, opacity: 0.65 });
          const sp = new THREE.Mesh(sG, sM);
          sp.rotation.z = (s * Math.PI) / 2;
          node.add(sp);
        }

        // Diamond shape at node
        const dGeo = new THREE.OctahedronGeometry(0.10, 0);
        const dMat = new THREE.MeshBasicMaterial({ color: T.c3, transparent: true, opacity: 0.7 });
        const diamond = new THREE.Mesh(dGeo, dMat);
        diamond.position.set(0.28, 0, 0);
        node.add(diamond);
      }

      // ── PARTICLE FIELD ──
      const N = 300;
      const pos = new Float32Array(N * 3);
      for (let i = 0; i < N; i++) {
        const r   = 2.5 + Math.random() * 7;
        const phi = Math.acos(2 * Math.random() - 1);
        const th  = Math.random() * Math.PI * 2;
        pos[i*3]   = r * Math.sin(phi) * Math.cos(th);
        pos[i*3+1] = r * Math.sin(phi) * Math.sin(th);
        pos[i*3+2] = r * Math.cos(phi);
      }
      const pGeo = new THREE.BufferGeometry();
      pGeo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
      const pMat = new THREE.PointsMaterial({
        color: T.c2, size: 0.055,
        transparent: true, opacity: 0.48,
        sizeAttenuation: true, depthWrite: false,
      });
      particles = new THREE.Points(pGeo, pMat);
      scene.add(particles);

      // ── ENERGY BEAMS ──
      beamLines = [];
      planNodes.forEach(node => {
        const bGeo = new THREE.BufferGeometry().setFromPoints([
          new THREE.Vector3(0,0,0), node.position.clone()
        ]);
        const bMat = new THREE.LineBasicMaterial({ color: T.c3, transparent: true, opacity: 0.22 });
        const beam = new THREE.Line(bGeo, bMat);
        scene.add(beam);
        beamLines.push({ line: beam, node });
      });

      // Grid floor
      const grid = new THREE.GridHelper(22, 28, T.c1, T.c1);
      grid.material.opacity = 0.06;
      grid.material.transparent = true;
      grid.position.y = -4.0;
      scene.add(grid);

      // Resize handler
      this._onResize = () => {
        if (!container || !renderer) return;
        const w = container.clientWidth  || 300;
        const h = container.clientHeight || 260;
        camera.aspect = w / h;
        camera.updateProjectionMatrix();
        renderer.setSize(w, h);
      };
      window.addEventListener('resize', this._onResize);

      this._animate(T);
    },

    _animate(T) {
      animFrame = requestAnimationFrame(() => this._animate(T));
      if (!renderer || !scene || !camera || !clock) return;
      const t = clock.getElapsedTime();
      clock.getDelta(); // consume

      // ── Core animation ──
      if (coreGroup) {
        const breathe = 1.0 + 0.065 * Math.sin(t * 1.85);
        coreGroup.scale.setScalar(breathe);
        coreGroup.rotation.y += 0.009;
        coreGroup.rotation.x  = 0.12 * Math.sin(t * 0.44);
        coreGroup.rotation.z  = 0.08 * Math.sin(t * 0.72);

        const cm = coreGroup.userData.coreMesh;
        if (cm && cm.material) cm.material.emissiveIntensity = T.glow * 0.55 + 0.25 * Math.sin(t * 2.1);

        const mm = coreGroup.userData.midMesh;
        if (mm) { mm.rotation.x += 0.019; mm.rotation.z -= 0.013; }

        const wm = coreGroup.userData.wireMesh;
        if (wm) { wm.rotation.y -= 0.011; wm.rotation.z += 0.008; }

        const xm = coreGroup.userData.xtalMesh;
        if (xm) { xm.rotation.x += 0.028; xm.rotation.y += 0.022; xm.rotation.z += 0.016; }
      }

      // ── Equatorial disc spin ──
      const disc = scene.userData.disc;
      if (disc) disc.rotation.z += 0.004;

      // ── Ring group slow Y rotation ──
      if (ringGroup) {
        ringGroup.rotation.y += 0.0025;
        ringGroup.children.forEach(child => {
          if (child.userData.isRing) child.rotation.z += child.userData.speed * 0.015;
        });
      }

      // ── Node orbits ──
      planNodes.forEach(node => {
        const a = node.userData.angle + t * node.userData.speed;
        const r = node.userData.rad;
        const tilt = node.userData.tilt;
        node.position.set(
          r * Math.cos(a),
          r * Math.sin(a) * Math.sin(tilt),
          r * Math.sin(a) * Math.cos(tilt)
        );
        node.rotation.y += 0.026;
        if (node.material) {
          node.material.emissiveIntensity = 0.7 + 0.4 * Math.sin(t * 2.6 + node.userData.idx * 1.2);
        }
      });

      // ── Beam updates ──
      beamLines.forEach(({ line, node }, i) => {
        const pts = [new THREE.Vector3(0,0,0), node.position.clone()];
        line.geometry.setFromPoints(pts);
        line.geometry.attributes.position.needsUpdate = true;
        if (line.material) line.material.opacity = 0.12 + 0.22 * Math.abs(Math.sin(t * 3.1 + i * 0.9));
      });

      // ── Particle drift ──
      if (particles) {
        particles.rotation.y += 0.0014;
        particles.rotation.x += 0.0005;
        if (particles.material) particles.material.opacity = 0.38 + 0.12 * Math.sin(t * 0.75);
      }

      // ── Camera gentle orbit ──
      const camR = 7.8;
      const cs   = 0.048;
      camera.position.x = camR * Math.sin(t * cs) * 0.42;
      camera.position.z = camR * Math.cos(t * cs) * 0.42 + 6.6;
      camera.position.y = 1.0 + 0.45 * Math.sin(t * 0.19);
      camera.lookAt(0, 0, 0);

      renderer.render(scene, camera);
    },

    highlightPlan(idx) {
      planNodes.forEach((n, i) => {
        if (!n.material) return;
        if (i === idx) {
          n.material.emissiveIntensity = 2.8;
          n.scale.setScalar(2.0);
        } else {
          n.scale.setScalar(1.0);
        }
      });
      beamLines.forEach(({ line }, i) => {
        if (line.material) line.material.opacity = (i === idx) ? 0.9 : 0.15;
      });
    },

    destroy() {
      if (animFrame) { cancelAnimationFrame(animFrame); animFrame = null; }
      if (this._onResize) window.removeEventListener('resize', this._onResize);
      if (renderer) {
        renderer.dispose();
        if (renderer.domElement && renderer.domElement.parentNode)
          renderer.domElement.parentNode.removeChild(renderer.domElement);
      }
      if (scene) {
        scene.traverse(obj => {
          if (obj.geometry) obj.geometry.dispose();
          if (obj.material) {
            if (Array.isArray(obj.material)) obj.material.forEach(m => m.dispose());
            else obj.material.dispose();
          }
        });
      }
      scene = camera = renderer = clock = null;
      coreGroup = ringGroup = particles = null;
      beamLines = []; planNodes = []; container = null;
    },
  };

})();