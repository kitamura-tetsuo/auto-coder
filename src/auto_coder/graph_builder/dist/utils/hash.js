"use strict";
/**
 * Hash utilities for generating unique IDs
 */
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.generateId = generateId;
exports.generateFileId = generateFileId;
const crypto = __importStar(require("crypto"));
/**
 * Generate a unique ID from fqname and signature
 * @param fqname Fully qualified name
 * @param sig Signature
 * @returns 16-character hex hash
 */
function generateId(fqname, sig) {
    const hash = crypto.createHash('sha1');
    hash.update(fqname + sig);
    return hash.digest('hex').slice(0, 16);
}
/**
 * Generate a unique ID for a file
 * @param path File path
 * @returns 16-character hex hash
 */
function generateFileId(path) {
    const hash = crypto.createHash('sha1');
    hash.update(path);
    return hash.digest('hex').slice(0, 16);
}
//# sourceMappingURL=hash.js.map