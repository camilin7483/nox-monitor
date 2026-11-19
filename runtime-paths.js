'use strict';

const path = require('node:path');
const DATA_DIR = path.resolve(process.env.NOX_DATA_DIR || __dirname);

module.exports = { DATA_DIR, PACKAGED: process.env.NOX_PACKAGED === '1' };
