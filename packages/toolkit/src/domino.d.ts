// @mixmark-io/domino ships typings under the module name 'domino', not under
// its published name. Only what markdown.ts uses.
declare module '@mixmark-io/domino' {
  const domino: {
    createDocument(html?: string, force?: boolean): Document;
  };
  export default domino;
}
