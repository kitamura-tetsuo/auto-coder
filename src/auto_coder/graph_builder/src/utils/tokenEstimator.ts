/**
 * Token estimation utilities
 */

/**
 * Estimate the number of tokens in a text
 * Simple heuristic: characters / 4
 * @param text Text to estimate
 * @returns Estimated token count
 */
export function estimateTokens(text: string): number {
  if (!text) return 0;
  return Math.ceil(text.length / 4);
}

/**
 * Calculate total tokens for a node
 * @param short Short summary
 * @param sig Signature
 * @returns Total estimated tokens
 */
export function calculateNodeTokens(short: string, sig: string): number {
  return estimateTokens(short) + estimateTokens(sig);
}

