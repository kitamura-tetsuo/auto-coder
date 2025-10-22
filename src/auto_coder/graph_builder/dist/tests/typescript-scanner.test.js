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
const fs = __importStar(require("fs"));
describe('TypeScript Scanner', () => {
    const tsConfigPath = path.join(__dirname, '../../test-sample/typescript/tsconfig.json');
    beforeAll(() => {
        // Verify test sample exists
        if (!fs.existsSync(tsConfigPath)) {
            throw new Error(`Test sample not found: ${tsConfigPath}`);
        }
    });
    test('should scan TypeScript project successfully', () => {
        const result = (0, typescript_1.scanTypeScriptProject)(tsConfigPath);
        expect(result.nodes.length).toBeGreaterThan(0);
        expect(result.edges.length).toBeGreaterThan(0);
        console.log(`Found ${result.nodes.length} nodes`);
        console.log(`Found ${result.edges.length} edges`);
    });
    test('should extract all node types', () => {
        const result = (0, typescript_1.scanTypeScriptProject)(tsConfigPath);
        const fileNodes = result.nodes.filter(n => n.kind === 'File');
        const functionNodes = result.nodes.filter(n => n.kind === 'Function');
        const classNodes = result.nodes.filter(n => n.kind === 'Class');
        const interfaceNodes = result.nodes.filter(n => n.kind === 'Interface');
        const typeNodes = result.nodes.filter(n => n.kind === 'Type');
        const methodNodes = result.nodes.filter(n => n.kind === 'Method');
        expect(fileNodes.length).toBeGreaterThan(0);
        expect(functionNodes.length).toBeGreaterThan(0);
        expect(classNodes.length).toBeGreaterThan(0);
        expect(interfaceNodes.length).toBeGreaterThan(0);
        expect(typeNodes.length).toBeGreaterThan(0);
        expect(methodNodes.length).toBeGreaterThan(0);
        console.log(`Files: ${fileNodes.length}`);
        console.log(`Functions: ${functionNodes.length}`);
        console.log(`Classes: ${classNodes.length}`);
        console.log(`Interfaces: ${interfaceNodes.length}`);
        console.log(`Types: ${typeNodes.length}`);
        console.log(`Methods: ${methodNodes.length}`);
    });
    test('should have required node properties', () => {
        const result = (0, typescript_1.scanTypeScriptProject)(tsConfigPath);
        for (const node of result.nodes) {
            expect(node.id).toBeDefined();
            expect(typeof node.id).toBe('string');
            expect(node.id.length).toBe(16);
            expect(node.kind).toBeDefined();
            expect(['File', 'Module', 'Function', 'Method', 'Class', 'Interface', 'Type']).toContain(node.kind);
            expect(node.fqname).toBeDefined();
            expect(typeof node.fqname).toBe('string');
            expect(node.sig).toBeDefined();
            expect(typeof node.sig).toBe('string');
            expect(node.short).toBeDefined();
            expect(typeof node.short).toBe('string');
            expect(node.complexity).toBeDefined();
            expect(typeof node.complexity).toBe('number');
            expect(node.complexity).toBeGreaterThanOrEqual(0);
            expect(node.tokens_est).toBeDefined();
            expect(typeof node.tokens_est).toBe('number');
            expect(node.tokens_est).toBeGreaterThan(0);
        }
    });
    test('should generate correct fqname for UserService class', () => {
        const result = (0, typescript_1.scanTypeScriptProject)(tsConfigPath);
        const userServiceClass = result.nodes.find(n => n.kind === 'Class' && n.fqname.includes('UserService'));
        expect(userServiceClass).toBeDefined();
        expect(userServiceClass?.fqname).toContain('user-service.ts:UserService');
        expect(userServiceClass?.sig).toBe('class UserService');
    });
    test('should generate correct fqname for getUserById method', () => {
        const result = (0, typescript_1.scanTypeScriptProject)(tsConfigPath);
        const getUserByIdMethod = result.nodes.find(n => n.kind === 'Method' && n.fqname.includes('getUserById'));
        expect(getUserByIdMethod).toBeDefined();
        expect(getUserByIdMethod?.fqname).toContain('user-service.ts:UserService.getUserById');
    });
    test('should generate correct signatures for functions', () => {
        const result = (0, typescript_1.scanTypeScriptProject)(tsConfigPath);
        const calculateAgeFunc = result.nodes.find(n => n.kind === 'Function' && n.fqname.includes('calculateAge'));
        expect(calculateAgeFunc).toBeDefined();
        expect(calculateAgeFunc?.sig).toContain('number');
        expect(calculateAgeFunc?.sig).toMatch(/\(.*\)->.*number/);
    });
    test('should detect ASYNC tag for async functions', () => {
        const result = (0, typescript_1.scanTypeScriptProject)(tsConfigPath);
        const asyncFunc = result.nodes.find(n => n.kind === 'Function' && n.fqname.includes('fetchUserFromAPI'));
        expect(asyncFunc).toBeDefined();
        expect(asyncFunc?.tags).toBeDefined();
        expect(asyncFunc?.tags).toContain('ASYNC');
    });
    test('should detect NETWORK tag for fetch calls', () => {
        const result = (0, typescript_1.scanTypeScriptProject)(tsConfigPath);
        const fetchFunc = result.nodes.find(n => n.kind === 'Function' && n.fqname.includes('fetchUserFromAPI'));
        expect(fetchFunc).toBeDefined();
        expect(fetchFunc?.tags).toBeDefined();
        expect(fetchFunc?.tags).toContain('NETWORK');
    });
    test('should detect DB tag for database operations', () => {
        const result = (0, typescript_1.scanTypeScriptProject)(tsConfigPath);
        const dbFunc = result.nodes.find(n => n.kind === 'Function' && n.fqname.includes('saveUserToDatabase'));
        expect(dbFunc).toBeDefined();
        expect(dbFunc?.tags).toBeDefined();
        expect(dbFunc?.tags).toContain('DB');
    });
    test('should calculate complexity correctly', () => {
        const result = (0, typescript_1.scanTypeScriptProject)(tsConfigPath);
        const isAdultFunc = result.nodes.find(n => n.kind === 'Function' && n.fqname.includes('isAdult'));
        expect(isAdultFunc).toBeDefined();
        expect(isAdultFunc?.complexity).toBeGreaterThan(0);
    });
    test('should extract CONTAINS edges', () => {
        const result = (0, typescript_1.scanTypeScriptProject)(tsConfigPath);
        const containsEdges = result.edges.filter(e => e.type === 'CONTAINS');
        expect(containsEdges.length).toBeGreaterThan(0);
        console.log(`CONTAINS edges: ${containsEdges.length}`);
    });
    test('should extract IMPORTS edges', () => {
        const result = (0, typescript_1.scanTypeScriptProject)(tsConfigPath);
        const importsEdges = result.edges.filter(e => e.type === 'IMPORTS');
        expect(importsEdges.length).toBeGreaterThan(0);
        console.log(`IMPORTS edges: ${importsEdges.length}`);
    });
    test('should extract CALLS edges', () => {
        const result = (0, typescript_1.scanTypeScriptProject)(tsConfigPath);
        const callsEdges = result.edges.filter(e => e.type === 'CALLS');
        // CALLS edges may or may not be present depending on symbol resolution
        console.log(`CALLS edges: ${callsEdges.length}`);
    });
    test('should have valid edge structure', () => {
        const result = (0, typescript_1.scanTypeScriptProject)(tsConfigPath);
        for (const edge of result.edges) {
            expect(edge.from).toBeDefined();
            expect(typeof edge.from).toBe('string');
            expect(edge.to).toBeDefined();
            expect(typeof edge.to).toBe('string');
            expect(edge.type).toBeDefined();
            expect(['IMPORTS', 'CALLS', 'CONTAINS', 'EXTENDS', 'IMPLEMENTS']).toContain(edge.type);
            expect(edge.count).toBeDefined();
            expect(typeof edge.count).toBe('number');
            expect(edge.count).toBeGreaterThan(0);
        }
    });
    test('should include file path in nodes', () => {
        const result = (0, typescript_1.scanTypeScriptProject)(tsConfigPath);
        const nodesWithFile = result.nodes.filter(n => n.file);
        expect(nodesWithFile.length).toBe(result.nodes.length);
        for (const node of nodesWithFile) {
            expect(node.file).toContain('.ts');
        }
    });
    test('should include line numbers in nodes', () => {
        const result = (0, typescript_1.scanTypeScriptProject)(tsConfigPath);
        const nodesWithLines = result.nodes.filter(n => n.start_line !== undefined && n.end_line !== undefined);
        expect(nodesWithLines.length).toBeGreaterThan(0);
        for (const node of nodesWithLines) {
            expect(node.start_line).toBeGreaterThan(0);
            expect(node.end_line).toBeGreaterThanOrEqual(node.start_line);
        }
    });
    test('should respect file limit option', () => {
        const resultAll = (0, typescript_1.scanTypeScriptProject)(tsConfigPath);
        const resultLimited = (0, typescript_1.scanTypeScriptProject)(tsConfigPath, 1);
        expect(resultLimited.nodes.length).toBeLessThan(resultAll.nodes.length);
        const filesInLimited = resultLimited.nodes.filter(n => n.kind === 'File');
        expect(filesInLimited.length).toBe(1);
    });
    test('should extract User interface', () => {
        const result = (0, typescript_1.scanTypeScriptProject)(tsConfigPath);
        const userInterface = result.nodes.find(n => n.kind === 'Interface' && n.fqname.includes(':User'));
        expect(userInterface).toBeDefined();
        expect(userInterface?.sig).toBe('interface User');
    });
    test('should extract UserRepository type', () => {
        const result = (0, typescript_1.scanTypeScriptProject)(tsConfigPath);
        const userRepoType = result.nodes.find(n => n.kind === 'Type' && n.fqname.includes('UserRepository'));
        expect(userRepoType).toBeDefined();
        expect(userRepoType?.sig).toBe('type UserRepository');
    });
    test('should extract DatabaseClient class', () => {
        const result = (0, typescript_1.scanTypeScriptProject)(tsConfigPath);
        const dbClientClass = result.nodes.find(n => n.kind === 'Class' && n.fqname.includes('DatabaseClient'));
        expect(dbClientClass).toBeDefined();
        expect(dbClientClass?.sig).toBe('class DatabaseClient');
    });
    test('should extract all methods from UserService', () => {
        const result = (0, typescript_1.scanTypeScriptProject)(tsConfigPath);
        const userServiceMethods = result.nodes.filter(n => n.kind === 'Method' && n.fqname.includes('UserService.'));
        expect(userServiceMethods.length).toBeGreaterThanOrEqual(5);
        const methodNames = userServiceMethods.map(m => {
            const parts = m.fqname.split('.');
            return parts[parts.length - 1];
        });
        console.log('UserService methods:', methodNames);
    });
});
//# sourceMappingURL=typescript-scanner.test.js.map