// Prediction cell formatting. Every cell: value + interval (or calibrated probability) + N + coverage / sample size.
export const fmtInt = (n) => n.toLocaleString("en-US");
export const fmtVal = (p, kind) => (kind === "classification" ? p.toFixed(2) : p.toFixed(1));

export function cellText(pred) {
  if (pred.kind === "regression") {
    return {
      main: `${fmtVal(pred.value, "regression")} [${fmtVal(pred.interval_low, "regression")}, ${fmtVal(pred.interval_high, "regression")}]`,
      sub: `${Math.round(pred.coverage * 100)}% interval | N=${pred.ensemble_n} | cal. n=${fmtInt(pred.sample_size)}`,
    };
  }
  return {
    main: `${pred.probability.toFixed(2)} (cal.)`,
    sub: `calibrated probability | N=${pred.ensemble_n} | n=${fmtInt(pred.sample_size)} near this score`,
  };
}

export function limitText(lim) {
  if (lim.max != null && lim.min == null) return `limit: < ${lim.max}`;
  if (lim.min != null && lim.max == null) return `limit: > ${lim.min}`;
  return `limit: ${lim.min} to ${lim.max}`;
}
