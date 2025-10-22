/**
 * Hash utilities for generating unique IDs
 */

import * as crypto from 'crypto';

/**
 * Generate a unique ID from fqname and signature
 * @param fqname Fully qualified name
 * @param sig Signature
 * @returns 16-character hex hash
 */
export function generateId(fqname: string, sig: string): string {
  const hash = crypto.createHash('sha1');
  hash.update(fqname + sig);
  return hash.digest('hex').slice(0, 16);
}

/**
 * Generate a unique ID for a file
 * @param path File path
 * @returns 16-character hex hash
 */
export function generateFileId(path: string): string {
  const hash = crypto.createHash('sha1');
  hash.update(path);
  return hash.digest('hex').slice(0, 16);
}

