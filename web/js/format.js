// Prediction cell formatting. Every cell: value + interval (or calibrated probability) + N + coverage / sample size.
export const fmtInt = (n) => Number(n).toLocaleString("en-US");
export const fmtVal = (v, kind) => (kind === "classification" ? Number(v).toFixed(2) : Number(v).toFixed(1));

// pred: the API's Prediction object (backend/schemas/contracts.py).
export function cellText(pred) {
  if (pred.kind === "regression") {
    return {
      main: `${fmtVal(pred.value, "regression")} [${fmtVal(pred.interval_low, "regression")}, ${fmtVal(pred.interval_high, "regression")}]`,
      sub: `${Math.round(pred.coverage * 100)}% interval | N=${pred.ensemble_n} | cal. n=${fmtInt(pred.sample_size)}`,
    };
  }
  return {
    main: `${Number(pred.probability).toFixed(2)} (cal.)`,
    sub: `calibrated probability | N=${pred.ensemble_n} | n=${fmtInt(pred.sample_size)} near this score`,
  };
}

export function limitText(lim) {
  if (lim.max != null && lim.min == null) return `limit: < ${lim.max}`;
  if (lim.min != null && lim.max == null) return `limit: > ${lim.min}`;
  return `limit: ${lim.min} to ${lim.max}`;
}

export const num = (v) => (v === "" || v == null || !Number.isFinite(Number(v)) ? null : Number(v));
