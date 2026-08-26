class CommaxVisitorCard extends HTMLElement {
  setConfig(config) {
    if (!config.entity) throw new Error("방문자 아카이브 센서 entity가 필요합니다.");
    this.config = config;
    this.page = 1;
    this.items = [];
    this.version = null;
    this.date = "";
    this.location = "";
    this.objectUrls = new Set();
    this.entityVersion = null;
    this.connect();
  }

  set hass(hass) {
    this._hass = hass;
    const state = this.config ? hass.states[this.config.entity] : null;
    const nextVersion = state
      ? `${state.state}:${state.attributes.archive_version ?? ""}`
      : null;
    const changed = this.entityVersion !== null && nextVersion !== this.entityVersion;
    this.entityVersion = nextVersion;
    this.connect();
    if (changed && this.api) this.load(true);
  }

  connect() {
    if (!this.config || !this._hass) return;
    const state = this._hass.states[this.config.entity];
    if (!state) {
      this.renderError(`엔티티를 찾을 수 없습니다: ${this.config.entity}`);
      return;
    }
    const api = state?.attributes?.api_url;
    if (!api) {
      this.renderError("방문자 API가 없습니다. commax_wallpad 0.9.1 설치와 HA 재시작을 확인하세요.");
      return;
    }
    if (api && api !== this.api) {
      this.api = api;
      this.renderShell();
      this.load(true);
      this.startPolling();
    }
  }

  renderError(message) {
    if (this.lastError === message) return;
    this.lastError = message;
    this.innerHTML = `<ha-card><div style="padding:20px;color:var(--error-color);font-weight:500">${message}</div></ha-card>`;
  }

  getCardSize() { return 6; }

  disconnectedCallback() {
    clearInterval(this.timer);
    this.objectUrls.forEach((url) => URL.revokeObjectURL(url));
  }

  renderShell() {
    this.lastError = null;
    this.innerHTML = `<ha-card><style>
      .wrap{padding:16px}.head{display:flex;justify-content:space-between;align-items:flex-end;gap:12px;flex-wrap:wrap}
      h2{margin:0;font-size:22px}.count{background:var(--primary-color);color:white;border-radius:99px;padding:2px 9px;font-size:14px}
      .tools{display:flex;gap:8px;align-items:center;flex-wrap:wrap}input,select,button{font:inherit;border:1px solid var(--divider-color);
      border-radius:8px;padding:8px 10px;background:var(--card-background-color);color:var(--primary-text-color)}button{cursor:pointer}
      .status{color:var(--secondary-text-color);margin:12px 0}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
      article{position:relative;overflow:hidden;border:1px solid var(--divider-color);border-radius:12px;background:var(--card-background-color)}
      .photo{display:block;width:100%;padding:0;border:0;border-radius:0;background:#111}.photo img{display:block;width:100%;aspect-ratio:4/3;object-fit:cover}
      .meta{padding:10px 12px;display:flex;flex-direction:column}.meta strong{font-size:16px}.meta span{color:var(--secondary-text-color)}
      .badge{position:absolute;right:7px;top:7px;display:flex;align-items:center;gap:4px;background:#17212bcc;color:white;
      border:1px solid #ffffff55;border-radius:99px;padding:4px 8px;font-size:12px}.badge ha-icon{--mdc-icon-size:15px}
      .more{display:block;margin:16px auto 0}.empty{text-align:center;padding:35px;color:var(--secondary-text-color)}
      .viewer{position:fixed;inset:0;background:#000d;z-index:9999;display:none;align-items:center;justify-content:center;flex-direction:column}
      .viewer.open{display:flex}.viewer img{max-width:94vw;max-height:82vh;border-radius:10px}.viewer p{color:white}.close{position:fixed;right:18px;top:18px;color:white;background:#0009;font-size:24px}
      @media(max-width:600px){.wrap{padding:12px}.tools{width:100%}.tools input,.tools select{flex:1;min-width:120px}
      .grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.meta{padding:7px 8px}.meta strong{font-size:14px}.meta span{font-size:11px}
      .badge{right:4px;top:4px;padding:3px 5px;font-size:10px}.badge ha-icon{--mdc-icon-size:12px}}
    </style><div class="wrap"><div class="head"><div><h2>방문자 기록 <span class="count">0</span></h2></div>
      <div class="tools"><input class="date" type="date"><select class="location"><option value="">전체 위치</option>
      <option value="공동현관">공동현관</option><option value="세대현관">세대현관</option></select><button class="clear">전체 보기</button></div></div>
      <p class="status">불러오는 중…</p><div class="grid"></div><button class="more" hidden>더 보기</button></div>
      <div class="viewer"><button class="close">×</button><img><p></p></div></ha-card>`;
    this.querySelector(".date").onchange = (e) => { this.date=e.target.value; this.load(true); };
    this.querySelector(".location").onchange = (e) => { this.location=e.target.value; this.load(true); };
    this.querySelector(".clear").onclick = () => { this.date="";this.location="";this.querySelector(".date").value="";
      this.querySelector(".location").value="";this.load(true); };
    this.querySelector(".more").onclick = () => { this.page++; this.load(false); };
    this.querySelector(".viewer").onclick = (e) => { if(e.target.classList.contains("viewer")||e.target.classList.contains("close")) this.closeViewer(); };
  }

  async load(reset) {
    if (!this.api || !this._hass) return;
    if (reset) { this.page=1; this.items=[]; }
    const query = new URLSearchParams({page:String(this.page),per_page:"30"});
    if (this.date) query.set("date",this.date);
    if (this.location) query.set("location",this.location);
    try {
      const data = await this._hass.callApi("GET", `${this.api}?${query}`);
      this.version = data.version;
      this.items.push(...data.items);
      await this.renderItems(data.total, data.has_more);
    } catch (err) {
      this.querySelector(".status").textContent = `방문자 기록을 불러오지 못했습니다: ${err}`;
    }
  }

  async renderItems(total, hasMore) {
    const grid=this.querySelector(".grid");
    if (this.page===1) { grid.innerHTML=""; this.objectUrls.forEach((u)=>URL.revokeObjectURL(u)); this.objectUrls.clear(); }
    const imageLoads=[];
    for (const item of this.items.slice(grid.children.length)) {
      const article=document.createElement("article");
      const date=item.captured_at.slice(0,10), time=item.captured_at.slice(11,19);
      const icon=item.location==="세대현관"?"mdi:home-account":item.location==="공동현관"?"mdi:door-open":"mdi:help-circle";
      article.innerHTML=`<button class="photo"><img alt="${date} ${time} 방문자 사진"></button><span class="badge"><ha-icon icon="${icon}"></ha-icon>${item.location}</span>
        <div class="meta"><strong>${time}</strong><span>${date}</span></div>`;
      grid.appendChild(article);
      imageLoads.push((async()=>{ try {
        const response = this._hass.callApiRaw
          ? await this._hass.callApiRaw("GET", item.image_url.replace(/^\/api\//,""))
          : await this._hass.fetchWithAuth(item.image_url);
        if (!response.ok) throw new Error(response.status);
        const objectUrl=URL.createObjectURL(await response.blob()); this.objectUrls.add(objectUrl);
        article.querySelector("img").src=objectUrl;
        article.querySelector(".photo").onclick=()=>this.openViewer(objectUrl,`${date} ${time} · ${item.location}`);
      } catch (_) { article.querySelector("img").alt="사진을 불러오지 못했습니다"; } })());
    }
    await Promise.all(imageLoads);
    this.querySelector(".count").textContent=total;
    this.querySelector(".status").textContent=this.date||this.location?[this.date,this.location].filter(Boolean).join(" · ")+` · ${total}건`:"최근 방문 순입니다.";
    this.querySelector(".more").hidden=!hasMore;
    if (!total) grid.innerHTML='<div class="empty">선택한 조건의 방문 기록이 없습니다.</div>';
  }

  openViewer(src, caption) { const v=this.querySelector(".viewer");v.querySelector("img").src=src;v.querySelector("p").textContent=caption;v.classList.add("open"); }
  closeViewer() { this.querySelector(".viewer")?.classList.remove("open"); }

  startPolling() {
    clearInterval(this.timer);
    this.timer=setInterval(async()=>{ if(document.hidden||!this.api)return; try {
      const q=new URLSearchParams({version_only:"1"});
      const data=await this._hass.callApi("GET",`${this.api}?${q}`);if(this.version&&data.version!==this.version)this.load(true);
    } catch (_) {} },30000);
  }
}

if (!customElements.get("commax-visitor-card")) customElements.define("commax-visitor-card", CommaxVisitorCard);
window.customCards = window.customCards || [];
window.customCards.push({type:"commax-visitor-card",name:"Commax 방문자 기록",description:"인증된 방문자 사진 아카이브"});
