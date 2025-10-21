/**
 * Tests for TypeScript scanner
 */

import { scanTypeScriptProject } from '../scanner/typescript';
import * as path from 'path';

describe('TypeScript Scanner', () => {
  it('should scan sample TypeScript project', () => {
    const tsConfigPath = path.join(__dirname, '../../sample-repo/typescript/tsconfig.json');
    const result = scanTypeScriptProject(tsConfigPath);

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
    const result = scanTypeScriptProject(tsConfigPath);

    const fetchUserFunc = result.nodes.find(n => 
      n.kind === 'Function' && n.fqname.includes('fetchUserFromAPI')
    );

    expect(fetchUserFunc).toBeDefined();
    expect(fetchUserFunc?.fqname).toContain('sample.ts:fetchUserFromAPI');
  });

  it('should generate correct signatures', () => {
    const tsConfigPath = path.join(__dirname, '../../sample-repo/typescript/tsconfig.json');
    const result = scanTypeScriptProject(tsConfigPath);

    const calculateAgeFunc = result.nodes.find(n => 
      n.kind === 'Function' && n.fqname.includes('calculateAge')
    );

    expect(calculateAgeFunc).toBeDefined();
    expect(calculateAgeFunc?.sig).toContain('number');
  });

  it('should detect tags correctly', () => {
    const tsConfigPath = path.join(__dirname, '../../sample-repo/typescript/tsconfig.json');
    const result = scanTypeScriptProject(tsConfigPath);

    const asyncFunc = result.nodes.find(n => 
      n.kind === 'Function' && n.fqname.includes('fetchUserFromAPI')
    );

    expect(asyncFunc).toBeDefined();
    expect(asyncFunc?.tags).toContain('ASYNC');
  });
});

