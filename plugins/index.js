'use strict';
/**
 * Nox Plugin Loader
 *
 * Scans the plugins/ directory for .js files (excluding itself and disabled/)
 * and loads each as a plugin module.
 *
 * Each plugin should export an object matching the Plugin API:
 *   { name, description?, onMessage?, onResponse?, onInterval?, apiRoutes? }
 */

const fs = require('fs');
const path = require('path');

const PLUGINS_DIR = __dirname;

/**
 * Load all plugins from the plugins directory.
 * @returns {Array<{name: string, description: string, onMessage: Function, onResponse: Function, onInterval: Function, apiRoutes: Array}>}
 */
function loadPlugins() {
  const plugins = [];
  try {
    const files = fs.readdirSync(PLUGINS_DIR);
    for (const file of files) {
      // Only load .js files, skip index.js and disabled/
      if (!file.endsWith('.js') || file === 'index.js') continue;
      // Skip if in a disabled prefix
      if (file.startsWith('_') || file.startsWith('.')) continue;
      try {
        const mod = require(path.join(PLUGINS_DIR, file));
        if (mod && mod.name) {
          plugins.push({
            name: mod.name,
            description: mod.description || '',
            onMessage: typeof mod.onMessage === 'function' ? mod.onMessage : null,
            onResponse: typeof mod.onResponse === 'function' ? mod.onResponse : null,
            onInterval: typeof mod.onInterval === 'function' ? mod.onInterval : null,
            apiRoutes: Array.isArray(mod.apiRoutes) ? mod.apiRoutes : [],
          });
          console.log(`[PLUGINS] Loaded: ${mod.name}`);
        }
      } catch (e) {
        console.error(`[PLUGINS] Error loading ${file}: ${e.message}`);
      }
    }
  } catch (e) {
    console.error(`[PLUGINS] Error scanning directory: ${e.message}`);
  }
  return plugins;
}

/**
 * Run onMessage hooks across all plugins.
 * Returns the first non-null modified message, or null.
 */
function runOnMessageHooks(plugins, context) {
  for (const p of plugins) {
    if (p.onMessage) {
      try {
        const result = p.onMessage(context);
        if (result !== null && result !== undefined) return result;
      } catch (e) {
        console.error(`[PLUGINS] ${p.name}.onMessage error: ${e.message}`);
      }
    }
  }
  return null;
}

/**
 * Run onResponse hooks across all plugins.
 * Returns the first non-null modified reply, or null.
 */
function runOnResponseHooks(plugins, context) {
  for (const p of plugins) {
    if (p.onResponse) {
      try {
        const result = p.onResponse(context);
        if (result !== null && result !== undefined) return result;
      } catch (e) {
        console.error(`[PLUGINS] ${p.name}.onResponse error: ${e.message}`);
      }
    }
  }
  return null;
}

/**
 * Get all API routes from all plugins.
 */
function getPluginRoutes(plugins) {
  const routes = [];
  for (const p of plugins) {
    for (const route of p.apiRoutes) {
      routes.push({ ...route, plugin: p.name });
    }
  }
  return routes;
}

module.exports = { loadPlugins, runOnMessageHooks, runOnResponseHooks, getPluginRoutes };
