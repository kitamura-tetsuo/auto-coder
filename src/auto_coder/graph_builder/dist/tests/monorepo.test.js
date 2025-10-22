"use strict";
/**
 * Tests for monorepo support in graph-builder
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
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
const child_process_1 = require("child_process");
describe('Monorepo Support', () => {
    const testMonorepoDir = path.join(__dirname, '../../test-monorepo');
    const outputDir = path.join(__dirname, '../../test-output-monorepo');
    beforeAll(() => {
        // Create a test monorepo structure
        if (fs.existsSync(testMonorepoDir)) {
            fs.rmSync(testMonorepoDir, { recursive: true });
        }
        fs.mkdirSync(testMonorepoDir, { recursive: true });
        // Create package1 with tsconfig.json
        const package1Dir = path.join(testMonorepoDir, 'packages', 'package1');
        fs.mkdirSync(package1Dir, { recursive: true });
        fs.writeFileSync(path.join(package1Dir, 'tsconfig.json'), JSON.stringify({
            compilerOptions: {
                target: 'ES2020',
                module: 'commonjs',
                strict: true,
            },
            include: ['src/**/*'],
        }, null, 2));
        const package1SrcDir = path.join(package1Dir, 'src');
        fs.mkdirSync(package1SrcDir, { recursive: true });
        fs.writeFileSync(path.join(package1SrcDir, 'index.ts'), `export function hello(): string {
  return 'Hello from package1';
}

export class Package1Service {
  greet(name: string): string {
    return \`Hello, \${name}!\`;
  }
}`);
        // Create package2 with tsconfig.json
        const package2Dir = path.join(testMonorepoDir, 'packages', 'package2');
        fs.mkdirSync(package2Dir, { recursive: true });
        fs.writeFileSync(path.join(package2Dir, 'tsconfig.json'), JSON.stringify({
            compilerOptions: {
                target: 'ES2020',
                module: 'commonjs',
                strict: true,
            },
            include: ['src/**/*'],
        }, null, 2));
        const package2SrcDir = path.join(package2Dir, 'src');
        fs.mkdirSync(package2SrcDir, { recursive: true });
        fs.writeFileSync(path.join(package2SrcDir, 'index.ts'), `export function world(): string {
  return 'World from package2';
}

export class Package2Service {
  farewell(name: string): string {
    return \`Goodbye, \${name}!\`;
  }
}`);
        // Create output directory
        if (fs.existsSync(outputDir)) {
            fs.rmSync(outputDir, { recursive: true });
        }
        fs.mkdirSync(outputDir, { recursive: true });
    });
    afterAll(() => {
        // Clean up test directories
        if (fs.existsSync(testMonorepoDir)) {
            fs.rmSync(testMonorepoDir, { recursive: true });
        }
        if (fs.existsSync(outputDir)) {
            fs.rmSync(outputDir, { recursive: true });
        }
    });
    test('should find multiple tsconfig.json files in monorepo', () => {
        const cliPath = path.join(__dirname, '../../dist/cli.js');
        // Run the CLI
        const result = (0, child_process_1.execSync)(`node ${cliPath} scan --project ${testMonorepoDir} --out ${outputDir} --languages typescript`, { encoding: 'utf-8' });
        // Check that multiple tsconfig.json files were found
        expect(result).toContain('Found 2 tsconfig.json file(s)');
        expect(result).toContain('Scanning TypeScript project:');
    });
    test('should extract nodes from all packages in monorepo', () => {
        const graphDataPath = path.join(outputDir, 'graph-data.json');
        expect(fs.existsSync(graphDataPath)).toBe(true);
        const graphData = JSON.parse(fs.readFileSync(graphDataPath, 'utf-8'));
        // Should have nodes from both packages
        expect(graphData.nodes.length).toBeGreaterThan(0);
        // Check for nodes from package1
        const package1Nodes = graphData.nodes.filter((node) => node.file && node.file.includes('package1'));
        expect(package1Nodes.length).toBeGreaterThan(0);
        // Check for nodes from package2
        const package2Nodes = graphData.nodes.filter((node) => node.file && node.file.includes('package2'));
        expect(package2Nodes.length).toBeGreaterThan(0);
        // Check for specific functions
        const helloFunction = graphData.nodes.find((node) => node.kind === 'Function' && node.fqname && node.fqname.includes('hello'));
        expect(helloFunction).toBeDefined();
        const worldFunction = graphData.nodes.find((node) => node.kind === 'Function' && node.fqname && node.fqname.includes('world'));
        expect(worldFunction).toBeDefined();
    });
    test('should handle monorepo without root tsconfig.json', () => {
        // This test verifies that the CLI works when there's no tsconfig.json at the root
        const cliPath = path.join(__dirname, '../../dist/cli.js');
        // Verify no root tsconfig.json exists
        const rootTsConfig = path.join(testMonorepoDir, 'tsconfig.json');
        expect(fs.existsSync(rootTsConfig)).toBe(false);
        // Run the CLI - should still work
        const result = (0, child_process_1.execSync)(`node ${cliPath} scan --project ${testMonorepoDir} --out ${outputDir} --languages typescript`, { encoding: 'utf-8' });
        expect(result).toContain('Found 2 tsconfig.json file(s)');
    });
    test('should prefer root tsconfig.json if it exists', () => {
        // Create a root tsconfig.json
        const rootTsConfig = path.join(testMonorepoDir, 'tsconfig.json');
        fs.writeFileSync(rootTsConfig, JSON.stringify({
            compilerOptions: {
                target: 'ES2020',
                module: 'commonjs',
            },
            include: ['packages/*/src/**/*'],
        }, null, 2));
        const cliPath = path.join(__dirname, '../../dist/cli.js');
        // Run the CLI
        const result = (0, child_process_1.execSync)(`node ${cliPath} scan --project ${testMonorepoDir} --out ${outputDir} --languages typescript`, { encoding: 'utf-8' });
        // Should find only 1 tsconfig.json (the root one)
        expect(result).toContain('Found 1 tsconfig.json file(s)');
        // Clean up
        fs.unlinkSync(rootTsConfig);
    });
});
//# sourceMappingURL=monorepo.test.js.map