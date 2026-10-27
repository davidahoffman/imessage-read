// Foundation archive decoder. Kept separate so archive exceptions cannot kill the reader.
import Foundation
let data = FileHandle.standardInput.readDataToEndOfFile()
let items = try JSONSerialization.jsonObject(with: data) as! [[String: Any]]
var output: [[String: Any]] = []
for item in items {
    var result: [String: Any] = ["id": item["id"]!]
    if let body = Data(base64Encoded: item["body"] as! String) {
        let object: Any?
        if body.starts(with: Data("bplist".utf8)) {
            object = try? NSKeyedUnarchiver.unarchivedObject(ofClasses: [NSAttributedString.self, NSMutableAttributedString.self, NSString.self, NSDictionary.self, NSArray.self, NSNumber.self, NSData.self, NSURL.self], from: body)
        } else {
            object = NSUnarchiver.unarchiveObject(with: body)
        }
        if let value = object as? NSAttributedString { result["text"] = value.string }
        else { result["error"] = "Archive is unsupported or not an attributed string" }
    } else { result["error"] = "Invalid base64" }
    output.append(result)
}
FileHandle.standardOutput.write(try JSONSerialization.data(withJSONObject: output, options: [.sortedKeys]))
