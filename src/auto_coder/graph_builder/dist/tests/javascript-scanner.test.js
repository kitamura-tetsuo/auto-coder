"use strict";
/**
 * Tests for JavaScript scanner
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
const javascript_1 = require("../scanner/javascript");
describe('JavaScript Scanner', () => {
    const testProjectDir = path.join(__dirname, '../../test-js-project');
    beforeAll(() => {
        // Create test JavaScript project
        if (fs.existsSync(testProjectDir)) {
            fs.rmSync(testProjectDir, { recursive: true });
        }
        fs.mkdirSync(testProjectDir, { recursive: true });
        // Create a simple JavaScript file
        fs.writeFileSync(path.join(testProjectDir, 'utils.js'), `/**
 * Utility functions
 */

/**
 * Add two numbers
 * @param {number} a - First number
 * @param {number} b - Second number
 * @returns {number} Sum of a and b
 */
function add(a, b) {
  return a + b;
}

/**
 * Subtract two numbers
 */
function subtract(a, b) {
  return a - b;
}

/**
 * User class
 */
class User {
  constructor(name, email) {
    this.name = name;
    this.email = email;
  }

  /**
   * Get user info
   */
  getInfo() {
    return \`\${this.name} <\${this.email}>\`;
  }

  /**
   * Validate email
   */
  validateEmail() {
    return this.email.includes('@');
  }
}

module.exports = { add, subtract, User };
`);
        // Create a JSX file
        fs.writeFileSync(path.join(testProjectDir, 'Component.jsx'), `/**
 * React component
 */

/**
 * Button component
 */
function Button({ label, onClick }) {
  return <button onClick={onClick}>{label}</button>;
}

/**
 * Card component
 */
class Card {
  constructor(props) {
    this.props = props;
  }

  render() {
    return <div className="card">{this.props.children}</div>;
  }
}

export { Button, Card };
`);
    });
    afterAll(() => {
        // Clean up
        if (fs.existsSync(testProjectDir)) {
            fs.rmSync(testProjectDir, { recursive: true });
        }
    });
    test('should scan JavaScript files', () => {
        const graphData = (0, javascript_1.scanJavaScriptProject)(testProjectDir);
        expect(graphData.nodes.length).toBeGreaterThan(0);
        expect(graphData.edges.length).toBeGreaterThan(0);
        console.log(`Found ${graphData.nodes.length} nodes`);
        console.log(`Found ${graphData.edges.length} edges`);
    });
    test('should extract functions from JavaScript files', () => {
        const graphData = (0, javascript_1.scanJavaScriptProject)(testProjectDir);
        const functions = graphData.nodes.filter(node => node.kind === 'Function');
        expect(functions.length).toBeGreaterThan(0);
        // Check for specific functions
        const addFunction = functions.find(f => f.fqname.includes('add'));
        expect(addFunction).toBeDefined();
        expect(addFunction?.sig).toContain('a');
        expect(addFunction?.sig).toContain('b');
        const subtractFunction = functions.find(f => f.fqname.includes('subtract'));
        expect(subtractFunction).toBeDefined();
    });
    test('should extract classes from JavaScript files', () => {
        const graphData = (0, javascript_1.scanJavaScriptProject)(testProjectDir);
        const classes = graphData.nodes.filter(node => node.kind === 'Class');
        expect(classes.length).toBeGreaterThan(0);
        // Check for User class
        const userClass = classes.find(c => c.fqname.includes('User'));
        expect(userClass).toBeDefined();
        // Check for Card class
        const cardClass = classes.find(c => c.fqname.includes('Card'));
        expect(cardClass).toBeDefined();
    });
    test('should extract methods from classes', () => {
        const graphData = (0, javascript_1.scanJavaScriptProject)(testProjectDir);
        const methods = graphData.nodes.filter(node => node.kind === 'Method');
        expect(methods.length).toBeGreaterThan(0);
        // Check for User methods
        const getInfoMethod = methods.find(m => m.fqname.includes('getInfo'));
        expect(getInfoMethod).toBeDefined();
        const validateEmailMethod = methods.find(m => m.fqname.includes('validateEmail'));
        expect(validateEmailMethod).toBeDefined();
    });
    test('should create CONTAINS edges', () => {
        const graphData = (0, javascript_1.scanJavaScriptProject)(testProjectDir);
        const containsEdges = graphData.edges.filter(edge => edge.type === 'CONTAINS');
        expect(containsEdges.length).toBeGreaterThan(0);
        console.log(`CONTAINS edges: ${containsEdges.length}`);
    });
    test('should handle JSX files', () => {
        const graphData = (0, javascript_1.scanJavaScriptProject)(testProjectDir);
        // Check for JSX components
        const buttonFunction = graphData.nodes.find(n => n.fqname.includes('Button'));
        expect(buttonFunction).toBeDefined();
        const cardClass = graphData.nodes.find(n => n.fqname.includes('Card'));
        expect(cardClass).toBeDefined();
    });
    test('should calculate complexity', () => {
        const graphData = (0, javascript_1.scanJavaScriptProject)(testProjectDir);
        const functions = graphData.nodes.filter(node => node.kind === 'Function');
        functions.forEach(func => {
            expect(func.complexity).toBeGreaterThanOrEqual(1);
        });
    });
    test('should estimate tokens', () => {
        const graphData = (0, javascript_1.scanJavaScriptProject)(testProjectDir);
        graphData.nodes.forEach(node => {
            expect(node.tokens_est).toBeGreaterThanOrEqual(0);
        });
    });
    test('should include file information', () => {
        const graphData = (0, javascript_1.scanJavaScriptProject)(testProjectDir);
        const functions = graphData.nodes.filter(node => node.kind === 'Function');
        functions.forEach(func => {
            expect(func.file).toBeDefined();
            expect(func.start_line).toBeGreaterThan(0);
            expect(func.end_line).toBeGreaterThanOrEqual(func.start_line);
        });
    });
    test('should generate short summaries', () => {
        const graphData = (0, javascript_1.scanJavaScriptProject)(testProjectDir);
        const addFunction = graphData.nodes.find(n => n.fqname.includes('add') && n.kind === 'Function');
        expect(addFunction).toBeDefined();
        expect(addFunction?.short).toBeTruthy();
        expect(addFunction?.short.length).toBeGreaterThan(0);
    });
});
//# sourceMappingURL=javascript-scanner.test.js.map