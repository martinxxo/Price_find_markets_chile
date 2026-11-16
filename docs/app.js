(() => {
  const EMBEDDED_FALLBACK_FEED = {
    generated_at: new Date().toISOString(),
    mode: "demo-fallback",
    total_monitored_products: 8,
    total_active_deals: 3,
    counts_by_type: { GLITCH: 1, VIP: 1, FLASH: 1 },
    deals: [
      {
        sku: "FAL-DEMO-LENOVO-1",
        store: "Falabella",
        title: "Notebook Lenovo IdeaPad Slim 3 15.6\" AMD Ryzen 5 16GB 512GB SSD",
        brand: "Lenovo",
        category: "notebooks",
        alert_type: "GLITCH",
        effective_price: 119990,
        normal_price: 649990,
        payment_type: "Tarjeta Tienda",
        previous_real_price: 529990,
        real_drop_pct: 77.4,
        savings_clp: 410000,
        reason: "Caída brusca real de 77.4% frente al historial ($529.990)",
        seller_name: "Falabella",
        url: "https://www.falabella.com/falabella-cl/product/demo-lenovo-slim3"
      },
      {
        sku: "PAR-DEMO-PS5-1",
        store: "Paris",
        title: "Consola PlayStation 5 Slim 1TB Edición Estándar + DualSense",
        brand: "Sony",
        category: "consolas",
        alert_type: "VIP",
        effective_price: 379990,
        normal_price: 599990,
        payment_type: "Tarjeta Tienda",
        previous_real_price: 519990,
        real_drop_pct: 26.9,
        savings_clp: 140000,
        reason: "Lista VIP 'PlayStation 5': bajo tu meta de $399.990",
        seller_name: "Paris",
        url: "https://www.paris.cl/demo-ps5-slim-1tb.html"
      },
      {
        sku: "FAL-DEMO-SONYXM5-1",
        store: "Falabella",
        title: "Audífonos Bluetooth Sony WH-1000XM5 Noise Cancelling Negro",
        brand: "Sony",
        category: "audio",
        alert_type: "FLASH",
        effective_price: 219990,
        normal_price: 399990,
        payment_type: "Tarjeta Tienda",
        previous_real_price: 339990,
        real_drop_pct: 35.3,
        savings_clp: 120000,
        reason: "Oferta Flash real: bajó 35.3% frente a su precio histórico",
        seller_name: "Falabella",
        url: "https://www.falabella.com/falabella-cl/product/demo-sony-xm5"
      }
    ]
  };

  const state = {
    deals: [],
    type: "ALL",
    store: "ALL",
    category: "ALL",
    search: "",
    sort: "drop_desc"
  };

  const els = {
    grid: document.getElementById("deals-grid"),
    emptyState: document.getElementById("empty-state"),
    resultsCount: document.getElementById("results-count"),
    lastUpdated: document.getElementById("last-updated-text"),
    modeBadge: document.getElementById("feed-mode-badge"),
    statMonitored: document.getElementById("stat-monitored"),
    statGlitches: document.getElementById("stat-glitches"),
    statFlash: document.getElementById("stat-flash"),
    statVip: document.getElementById("stat-vip"),
    searchInput: document.getElementById("search-input"),
    storeSelect: document.getElementById("store-select"),
    categorySelect: document.getElementById("category-select"),
    sortSelect: document.getElementById("sort-select"),
    resetBtn: document.getElementById("reset-filters-btn"),
    themeToggle: document.getElementById("theme-toggle"),
    toast: document.getElementById("toast")
  };

  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function formatClp(amount) {
    const num = Math.round(Number(amount) || 0);
    return "$" + num.toLocaleString("es-CL");
  }

  function formatRelativeTime(isoString) {
    if (!isoString) return "Sin fecha";
    const dt = new Date(isoString);
    if (Number.isNaN(dt.getTime())) return "Actualizado";
    return "Actualizado: " + dt.toLocaleString("es-CL", {
      dateStyle: "short",
      timeStyle: "short"
    });
  }

  function showToast(message) {
    els.toast.textContent = message;
    els.toast.classList.remove("hidden");
    clearTimeout(showToast._timer);
    showToast._timer = setTimeout(() => {
      els.toast.classList.add("hidden");
    }, 2600);
  }

  function getUrgencyLabel(type) {
    if (type === "GLITCH") return "🚨 POSIBLE ERROR DE PRECIO";
    if (type === "VIP") return "⭐ META VIP";
    return "⚡ OFERTA FLASH";
  }

  function getPaymentBadge(paymentType) {
    if (paymentType === "Tarjeta Tienda") return "💳 Tarjeta Tienda";
    return "💵 Todo Medio de Pago";
  }

  function getFilteredDeals() {
    const query = state.search.trim().toLowerCase();
    const filtered = state.deals.filter((deal) => {
      if (state.type !== "ALL" && deal.alert_type !== state.type) return false;
      if (state.store !== "ALL" && deal.store !== state.store) return false;
      if (state.category !== "ALL" && deal.category !== state.category) return false;
      if (query) {
        const hay = `${deal.title} ${deal.brand} ${deal.store} ${deal.sku}`.toLowerCase();
        if (!hay.includes(query)) return false;
      }
      return true;
    });

    filtered.sort((a, b) => {
      if (state.sort === "savings_desc") return (b.savings_clp || 0) - (a.savings_clp || 0);
      if (state.sort === "price_asc") return (a.effective_price || 0) - (b.effective_price || 0);
      return (b.real_drop_pct || 0) - (a.real_drop_pct || 0);
    });

    return filtered;
  }

  function renderDeals() {
    const filtered = getFilteredDeals();
    els.resultsCount.textContent = `Mostrando ${filtered.length} de ${state.deals.length} ofertas activas`;

    if (filtered.length === 0) {
      els.grid.innerHTML = "";
      els.emptyState.classList.remove("hidden");
      return;
    }

    els.emptyState.classList.add("hidden");
    els.grid.innerHTML = filtered
      .map((deal) => {
        const type = escapeHtml(deal.alert_type);
        const title = escapeHtml(deal.title);
        const store = escapeHtml(deal.store);
        const seller = escapeHtml(deal.seller_name || deal.store);
        const category = escapeHtml(deal.category);
        const reason = escapeHtml(deal.reason);
        const url = escapeHtml(deal.url || "#");
        const dropPct = Number(deal.real_drop_pct || 0).toFixed(1);
        const hasNormal = deal.normal_price && deal.normal_price > deal.previous_real_price;

        return `
          <article class="deal-card type-${type}">
            <div>
              <div class="card-top">
                <span class="urgency-badge badge-${type}">${getUrgencyLabel(deal.alert_type)}</span>
                <span class="store-tag">${store}</span>
              </div>
              <h2 class="deal-title" style="margin-top: 10px;">${title}</h2>
              <div class="deal-meta-tags" style="margin-top: 8px;">
                <span class="meta-chip">${getPaymentBadge(deal.payment_type)}</span>
                <span class="meta-chip">Vendido por: ${seller}</span>
                <span class="meta-chip">#${category}</span>
              </div>
            </div>

            <div class="price-box">
              <div class="price-main-row">
                <span class="current-price">${formatClp(deal.effective_price)}</span>
                <span class="drop-pill">-${dropPct}% real</span>
              </div>
              <div class="price-sub-row">
                <span>Antes (historial real): <strong class="strike">${formatClp(deal.previous_real_price)}</strong></span>
                ${hasNormal ? `<span>Precio normal tienda: <span class="strike">${formatClp(deal.normal_price)}</span></span>` : ""}
                <span>Ahorro estimado: <strong>${formatClp(deal.savings_clp)}</strong></span>
              </div>
            </div>

            <p class="deal-reason">${reason}</p>

            <div class="card-actions">
              <a class="btn-primary" href="${url}" target="_blank" rel="noopener noreferrer">
                Comprar en Tienda ↗
              </a>
              <button
                type="button"
                class="btn-secondary share-btn"
                data-sku="${escapeHtml(deal.sku)}"
                title="Compartir oferta"
              >
                Compartir
              </button>
            </div>
          </article>
        `;
      })
      .join("");
  }

  function applyFeed(feed) {
    state.deals = Array.isArray(feed.deals) ? feed.deals : [];
    const counts = feed.counts_by_type || {};

    els.statMonitored.textContent = Number(feed.total_monitored_products || 0).toLocaleString("es-CL");
    els.statGlitches.textContent = Number(counts.GLITCH || 0).toLocaleString("es-CL");
    els.statFlash.textContent = Number(counts.FLASH || 0).toLocaleString("es-CL");
    els.statVip.textContent = Number(counts.VIP || 0).toLocaleString("es-CL");
    els.lastUpdated.textContent = formatRelativeTime(feed.generated_at);
    els.modeBadge.textContent = feed.mode === "scan" ? "Modo: Escaneo Real" : `Modo: ${feed.mode || "Demo"}`;

    renderDeals();
  }

  async function loadFeed() {
    try {
      const response = await fetch("data/deals.json", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      applyFeed(data);
    } catch (_err) {
      applyFeed(EMBEDDED_FALLBACK_FEED);
    }
  }

  function bindEvents() {
    document.querySelectorAll(".type-pills .pill").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".type-pills .pill").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        state.type = btn.dataset.type || "ALL";
        renderDeals();
      });
    });

    els.searchInput.addEventListener("input", (e) => {
      state.search = e.target.value;
      renderDeals();
    });

    els.storeSelect.addEventListener("change", (e) => {
      state.store = e.target.value;
      renderDeals();
    });

    els.categorySelect.addEventListener("change", (e) => {
      state.category = e.target.value;
      renderDeals();
    });

    els.sortSelect.addEventListener("change", (e) => {
      state.sort = e.target.value;
      renderDeals();
    });

    els.resetBtn.addEventListener("click", () => {
      state.type = "ALL";
      state.store = "ALL";
      state.category = "ALL";
      state.search = "";
      state.sort = "drop_desc";
      els.searchInput.value = "";
      els.storeSelect.value = "ALL";
      els.categorySelect.value = "ALL";
      els.sortSelect.value = "drop_desc";
      document.querySelectorAll(".type-pills .pill").forEach((b) => {
        b.classList.toggle("active", b.dataset.type === "ALL");
      });
      renderDeals();
    });

    els.grid.addEventListener("click", async (e) => {
      const btn = e.target.closest(".share-btn");
      if (!btn) return;
      const sku = btn.dataset.sku;
      const deal = state.deals.find((d) => d.sku === sku);
      if (!deal) return;

      const shareText = `${getUrgencyLabel(deal.alert_type)} en ${deal.store}: ${deal.title} a ${formatClp(deal.effective_price)} (-${Number(deal.real_drop_pct || 0).toFixed(1)}%) ${deal.url}`;
      if (navigator.share) {
        try {
          await navigator.share({ title: deal.title, text: shareText, url: deal.url });
          return;
        } catch (_ignored) {
          // User cancelled or share unsupported, fallback to clipboard
        }
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        try {
          await navigator.clipboard.writeText(shareText);
          showToast("¡Oferta copiada al portapapeles!");
          return;
        } catch (_ignored) {
          // Fallback to WhatsApp share URL
        }
      }
      window.open(`https://wa.me/?text=${encodeURIComponent(shareText)}`, "_blank", "noopener");
    });

    els.themeToggle.addEventListener("click", () => {
      const root = document.documentElement;
      const next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      localStorage.setItem("chiledeals-theme", next);
    });

    const savedTheme = localStorage.getItem("chiledeals-theme");
    if (savedTheme === "light" || savedTheme === "dark") {
      document.documentElement.setAttribute("data-theme", savedTheme);
    }
  }

  bindEvents();
  loadFeed();
})();
