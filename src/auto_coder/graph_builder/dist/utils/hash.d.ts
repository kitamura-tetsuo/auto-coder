/**
 * Hash utilities for generating unique IDs
 */
/**
 * Generate a unique ID from fqname and signature
 * @param fqname Fully qualified name
 * @param sig Signature
 * @returns 16-character hex hash
 */
export declare function generateId(fqname: string, sig: string): string;
/**
 * Generate a unique ID for a file
 * @param path File path
 * @returns 16-character hex hash
 */
export declare function generateFileId(path: string): string;
//# sourceMappingURL=hash.d.ts.map