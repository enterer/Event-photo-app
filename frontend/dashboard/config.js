// Fill these in from the stack outputs:
//   aws cloudformation describe-stacks --stack-name eventphotos-dev --region us-east-1 --query "Stacks[0].Outputs" --output table
window.PLATFORM_CONFIG = {
  apiUrl: "https://16xczslrj3.execute-api.us-east-1.amazonaws.com",  // ApiUrl
  region: "us-east-1",
  userPoolClientId: "4b57ag0hbnh31thajehpneoq5i",                                   // UserPoolClientId
  guestBaseUrl: "",  // where guest pages will live once built; leave empty for now
};
