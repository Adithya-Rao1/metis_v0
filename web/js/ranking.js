// Ranking math (spec 7). Pure functions, no DOM. R_j = (1-lam) S_j + lam C_j,
// S_j = sum_i w_i d_ij / sum_i w_i, C_j = sum_i w_i c_ij / sum_i w_i.
// d_ij: desirability in [0,1]; c_ij: confidence in [0,1].

export const clamp01 = (x) => Math.min(1, Math.max(0, x));

export function binaryEntropyBits(p) {
  if (p <= 0 || p >= 1) return 0;
  return -(p * Math.log2(p) + (1 - p) * Math.log2(1 - p));
}
// regression: c = max(0, 1 - width / (2 * median calibration width))
export const confidenceRegression = (width, medianCalWidth) => Math.max(0, 1 - width / (2 * medianCalWidth));
// classification: c = 1 - binary entropy (bits)
export const confidenceClassification = (p) => 1 - binaryEntropyBits(p);

// info = { kind, range:[lo,hi], cal_width }; weight entry gives higher_is_better.
export function desirability(pred, info, higherIsBetter) {
  if (info.kind === "classification") return higherIsBetter ? pred.probability : 1 - pred.probability;
  const [lo, hi] = info.range;
  const t = clamp01((pred.value - lo) / (hi - lo));
  return higherIsBetter ? t : 1 - t;
}
export function confidence(pred, info) {
  if (info.kind === "classification") return confidenceClassification(pred.probability);
  return confidenceRegression(pred.interval_high - pred.interval_low, info.cal_width);
}

// Value a hard limit is checked against: point estimate, or the unfavorable interval bound.
export function limitValue(pred, info, limit) {
  if (info.kind === "classification" || !limit.use_unfavorable_bound) return info.kind === "classification" ? pred.probability : pred.value;
  // unfavorable = the bound nearest to violating: lower bound for a min limit, upper bound for a max limit
  if (limit.min != null && limit.max == null) return pred.interval_low;
  if (limit.max != null && limit.min == null) return pred.interval_high;
  // both set: report whichever bound is further outside the allowed window
  const lowViol = limit.min - pred.interval_low, highViol = pred.interval_high - limit.max;
  return lowViol >= highViol ? pred.interval_low : pred.interval_high;
}
export function failedLimits(compound, config, infos) {
  const out = [];
  for (const lim of config.limits || []) {
    const pred = compound.predictions[lim.endpoint];
    if (!pred) continue;
    const v = limitValue(pred, infos[lim.endpoint], lim);
    if ((lim.min != null && v < lim.min) || (lim.max != null && v > lim.max)) out.push(lim.endpoint);
  }
  return out;
}

// compounds: [{id, predictions:{endpoint:{value, interval_low, interval_high, probability}}}]
// config: {weights:[{endpoint,weight,higher_is_better}], limits:[...], lam}
// infos: {endpoint:{kind, range, cal_width}}
export function scoreAll(compounds, config, infos) {
  const W = config.weights.reduce((a, w) => a + w.weight, 0);
  if (!(W > 0)) throw new Error("at least one weight must be positive");
  const lam = config.lam;
  const rows = compounds.map((c, index) => {
    let S = 0, C = 0;
    const parts = [];
    for (const w of config.weights) {
      const pred = c.predictions[w.endpoint], info = infos[w.endpoint];
      const d = desirability(pred, info, w.higher_is_better), cc = confidence(pred, info);
      S += (w.weight * d) / W; C += (w.weight * cc) / W;
      parts.push({ endpoint: w.endpoint, d, c: cc, share: (w.weight / W) * ((1 - lam) * d + lam * cc) });
    }
    const failed = failedLimits(c, config, infos);
    return { id: c.id, index, S, C, R: (1 - lam) * S + lam * C, parts, failed, passes: failed.length === 0 };
  });
  // limit-failers kept, marked, ranked below passers; ties broken by input order
  const order = (key) => rows.slice().sort((a, b) => (a.passes === b.passes ? (b[key] - a[key]) || (a.index - b.index) : a.passes ? -1 : 1));
  order("R").forEach((r, i) => { r.blendedRank = i + 1; });
  order("S").forEach((r, i) => { r.propertyOnlyRank = i + 1; });
  return rows;
}

export const FORMULA = "R_j = (1 - lambda) S_j + lambda C_j;  S_j = sum_i w_i d_ij / sum_i w_i;  C_j = sum_i w_i c_ij / sum_i w_i";
