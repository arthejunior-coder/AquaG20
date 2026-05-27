/**
 * Máscara de moeda BR pra inputs marcados com data-money.
 *
 * Comportamento:
 *   - Aceita só dígitos, vírgula e ponto durante digitação
 *   - No blur, formata como "1.234,56" (ponto milhar, vírgula decimal)
 *   - Backend (BrlMoneyField + parse_money) aceita esse formato direto
 *
 * Uso no template:
 *   <input type="text" data-money inputmode="decimal" />
 *
 * Vanilla JS, sem dep. Idempotente — pode rodar várias vezes sem dar bug.
 */
(function () {
  "use strict";

  function formatBRL(numStr) {
    // numStr no formato "1234.56" (US) — converte pra "1.234,56"
    if (!numStr) return "";
    const num = parseFloat(numStr);
    if (Number.isNaN(num)) return numStr;
    // toLocaleString pt-BR já faz a formatação:
    return num.toLocaleString("pt-BR", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  }

  function normalizeToDot(s) {
    // Converte "1.234,56" → "1234.56" pra parseFloat (mesma lógica do backend)
    if (!s) return "";
    const cleaned = s.replace(/[^\d.,-]/g, "");
    const hasComma = cleaned.includes(",");
    const hasDot = cleaned.includes(".");
    if (hasComma && hasDot) {
      if (cleaned.lastIndexOf(",") > cleaned.lastIndexOf(".")) {
        return cleaned.replace(/\./g, "").replace(",", ".");
      } else {
        return cleaned.replace(/,/g, "");
      }
    }
    if (hasComma) return cleaned.replace(",", ".");
    return cleaned;
  }

  function attachMoneyMask(input) {
    if (input.dataset.moneyAttached === "1") return;
    input.dataset.moneyAttached = "1";

    // Format on blur
    input.addEventListener("blur", function () {
      const normalized = normalizeToDot(input.value);
      if (normalized === "" || normalized === "-") {
        input.value = "";
        return;
      }
      const formatted = formatBRL(normalized);
      input.value = formatted;
    });

    // Format value already in the field at page load
    if (input.value) {
      const normalized = normalizeToDot(input.value);
      if (normalized) input.value = formatBRL(normalized);
    }
  }

  function init() {
    document.querySelectorAll("input[data-money]").forEach(attachMoneyMask);
  }

  // Initial pass + observe novas linhas adicionadas via HTMX
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // HTMX swap → reattach
  document.body.addEventListener("htmx:afterSwap", init);
})();
