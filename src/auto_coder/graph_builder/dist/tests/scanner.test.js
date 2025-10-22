"use strict";
/**
 * Tests for TypeScript scanner
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
const typescript_1 = require("../scanner/typescript");
const path = __importStar(require("path"));
describe('TypeScript Scanner', () => {
    it('should scan sample TypeScript project', () => {
        const tsConfigPath = path.join(__dirname, '../../sample-repo/typescript/tsconfig.json');
        const result = (0, typescript_1.scanTypeScriptProject)(tsConfigPath);
        expect(result.nodes.length).toBeGreaterThan(0);
        expect(result.edges.length).toBeGreaterThan(0);
        // Check for expected nodes
        const functionNodes = result.nodes.filter(n => n.kind === 'Function');
        const classNodes = result.nodes.filter(n => n.kind === 'Class');
        const interfaceNodes = result.nodes.filter(n => n.kind === 'Interface');
        expect(functionNodes.length).toBeGreaterThan(0);
        expect(classNodes.length).toBeGreaterThan(0);
        expect(interfaceNodes.length).toBeGreaterThan(0);
        // Check node properties
        for (const node of result.nodes) {
            expect(node.id).toBeDefined();
            expect(node.kind).toBeDefined();
            expect(node.fqname).toBeDefined();
            expect(node.sig).toBeDefined();
            expect(node.short).toBeDefined();
            expect(node.complexity).toBeGreaterThanOrEqual(0);
            expect(node.tokens_est).toBeGreaterThan(0);
        }
    });
    it('should generate correct fqname for functions', () => {
        const tsConfigPath = path.join(__dirname, '../../sample-repo/typescript/tsconfig.json');
        const result = (0, typescript_1.scanTypeScriptProject)(tsConfigPath);
        const fetchUserFunc = result.nodes.find(n => n.kind === 'Function' && n.fqname.includes('fetchUserFromAPI'));
        expect(fetchUserFunc).toBeDefined();
        expect(fetchUserFunc?.fqname).toContain('sample.ts:fetchUserFromAPI');
    });
    it('should generate correct signatures', () => {
        const tsConfigPath = path.join(__dirname, '../../sample-repo/typescript/tsconfig.json');
        const result = (0, typescript_1.scanTypeScriptProject)(tsConfigPath);
        const calculateAgeFunc = result.nodes.find(n => n.kind === 'Function' && n.fqname.includes('calculateAge'));
        expect(calculateAgeFunc).toBeDefined();
        expect(calculateAgeFunc?.sig).toContain('number');
    });
    it('should detect tags correctly', () => {
        const tsConfigPath = path.join(__dirname, '../../sample-repo/typescript/tsconfig.json');
        const result = (0, typescript_1.scanTypeScriptProject)(tsConfigPath);
        const asyncFunc = result.nodes.find(n => n.kind === 'Function' && n.fqname.includes('fetchUserFromAPI'));
        expect(asyncFunc).toBeDefined();
        expect(asyncFunc?.tags).toContain('ASYNC');
    });
});
//# sourceMappingURL=scanner.test.js.map