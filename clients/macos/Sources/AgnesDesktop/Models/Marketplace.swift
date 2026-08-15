import Foundation

/// Source shelves exposed by `agnes marketplace search`.
public enum MarketplaceSource: Hashable, Sendable, Codable {
  case curated
  case flea
  case unknown(String)

  public var rawValue: String {
    switch self {
    case .curated: "curated"
    case .flea: "flea"
    case .unknown(let value): value
    }
  }

  public init(from decoder: Decoder) throws {
    let value = try decoder.singleValueContainer().decode(String.self)
    switch value {
    case "curated": self = .curated
    case "flea": self = .flea
    default: self = .unknown(value)
    }
  }

  public func encode(to encoder: Encoder) throws {
    var container = encoder.singleValueContainer()
    try container.encode(rawValue)
  }
}

/// Item kinds accepted by the Agnes marketplace filter.
public enum MarketplaceItemType: Hashable, Sendable, Codable {
  case skill
  case agent
  case plugin
  case unknown(String)

  public var rawValue: String {
    switch self {
    case .skill: "skill"
    case .agent: "agent"
    case .plugin: "plugin"
    case .unknown(let value): value
    }
  }

  public init(from decoder: Decoder) throws {
    let value = try decoder.singleValueContainer().decode(String.self)
    switch value {
    case "skill": self = .skill
    case "agent": self = .agent
    case "plugin": self = .plugin
    default: self = .unknown(value)
    }
  }

  public func encode(to encoder: Encoder) throws {
    var container = encoder.singleValueContainer()
    try container.encode(rawValue)
  }
}

/// Sort modes documented by `agnes marketplace search --help`.
public enum MarketplaceSort: String, CaseIterable, Hashable, Sendable, Codable {
  case recent
  case mostUsed = "most_used"
  case trending
}

/// The JSON envelope emitted by `agnes marketplace search --json`.
public struct MarketplaceSearchResult: Equatable, Sendable, Codable {
  public let items: [MarketplaceItem]
  public let total: Int

  public init(items: [MarketplaceItem], total: Int? = nil) {
    self.items = items
    self.total = total ?? items.count
  }

  private enum CodingKeys: String, CodingKey {
    case items, total
  }

  public init(from decoder: Decoder) throws {
    let container = try decoder.container(keyedBy: CodingKeys.self)
    items = try container.decode([MarketplaceItem].self, forKey: .items)
    total = container.intValue(forKey: .total) ?? items.count
  }
}

/// A marketplace card. Optional fields are intentionally tolerant because
/// curated and Flea cards expose different metadata.
public struct MarketplaceItem: Equatable, Hashable, Identifiable, Sendable, Codable {
  public let id: String
  public let source: MarketplaceSource
  public let type: MarketplaceItemType
  public let name: String
  public let displayName: String?
  public let description: String?
  public let tagline: String?
  public let owner: String?
  public let version: String?
  public let category: String?
  public let photoURL: String?
  public let added: String?
  public let installed: Bool
  public let marketplaceSlug: String?
  public let marketplaceName: String?
  public let detailURL: String?
  public let visibilityStatus: String?
  public let isViewerOwner: Bool
  public let isSystem: Bool
  public let isRequired: Bool
  public let invocations30d: Int
  public let distinctUsers30d: Int
  public let trendPercent: Double?
  public let stackCount: Int

  private enum CodingKeys: String, CodingKey {
    case id, source, type, name, description, tagline, owner, version, category, added, installed
    case displayName = "display_name"
    case photoURL = "photo_url"
    case marketplaceSlug = "marketplace_slug"
    case marketplaceName = "marketplace_name"
    case detailURL = "detail_url"
    case visibilityStatus = "visibility_status"
    case isViewerOwner = "is_viewer_owner"
    case isSystem = "is_system"
    case isRequired = "is_required"
    case invocations30d = "invocations_30d"
    case distinctUsers30d = "distinct_users_30d"
    case trendPercent = "trend_pct"
    case stackCount = "stack_count"
  }

  public init(
    id: String,
    source: MarketplaceSource,
    type: MarketplaceItemType,
    name: String,
    displayName: String? = nil,
    description: String? = nil,
    tagline: String? = nil,
    owner: String? = nil,
    version: String? = nil,
    category: String? = nil,
    photoURL: String? = nil,
    added: String? = nil,
    installed: Bool = false,
    marketplaceSlug: String? = nil,
    marketplaceName: String? = nil,
    detailURL: String? = nil,
    visibilityStatus: String? = nil,
    isViewerOwner: Bool = false,
    isSystem: Bool = false,
    isRequired: Bool = false,
    invocations30d: Int = 0,
    distinctUsers30d: Int = 0,
    trendPercent: Double? = nil,
    stackCount: Int = 0
  ) {
    self.id = id
    self.source = source
    self.type = type
    self.name = name
    self.displayName = displayName
    self.description = description
    self.tagline = tagline
    self.owner = owner
    self.version = version
    self.category = category
    self.photoURL = photoURL
    self.added = added
    self.installed = installed
    self.marketplaceSlug = marketplaceSlug
    self.marketplaceName = marketplaceName
    self.detailURL = detailURL
    self.visibilityStatus = visibilityStatus
    self.isViewerOwner = isViewerOwner
    self.isSystem = isSystem
    self.isRequired = isRequired
    self.invocations30d = invocations30d
    self.distinctUsers30d = distinctUsers30d
    self.trendPercent = trendPercent
    self.stackCount = stackCount
  }

  public init(from decoder: Decoder) throws {
    let container = try decoder.container(keyedBy: CodingKeys.self)
    id = container.stringValue(forKey: .id) ?? ""
    source = (try? container.decode(MarketplaceSource.self, forKey: .source)) ?? .unknown("")
    type = (try? container.decode(MarketplaceItemType.self, forKey: .type)) ?? .unknown("")
    name = container.stringValue(forKey: .name) ?? ""
    displayName = container.stringValue(forKey: .displayName)
    description = container.stringValue(forKey: .description)
    tagline = container.stringValue(forKey: .tagline)
    owner = container.stringValue(forKey: .owner)
    version = container.stringValue(forKey: .version)
    category = container.stringValue(forKey: .category)
    photoURL = container.stringValue(forKey: .photoURL)
    added = container.stringValue(forKey: .added)
    installed = container.boolValue(forKey: .installed) ?? false
    marketplaceSlug = container.stringValue(forKey: .marketplaceSlug)
    marketplaceName = container.stringValue(forKey: .marketplaceName)
    detailURL = container.stringValue(forKey: .detailURL)
    visibilityStatus = container.stringValue(forKey: .visibilityStatus)
    isViewerOwner = container.boolValue(forKey: .isViewerOwner) ?? false
    isSystem = container.boolValue(forKey: .isSystem) ?? false
    isRequired = container.boolValue(forKey: .isRequired) ?? false
    invocations30d = container.intValue(forKey: .invocations30d) ?? 0
    distinctUsers30d = container.intValue(forKey: .distinctUsers30d) ?? 0
    trendPercent = container.doubleValue(forKey: .trendPercent)
    stackCount = container.intValue(forKey: .stackCount) ?? 0
  }
}

/// Stack-membership cards emitted by `agnes my-stack show --json`.
///
/// The CLI response intentionally has different curated and Store shapes.
/// This type normalizes only entries that are actually in the caller's stack
/// into the same cards the marketplace search screen already understands.
public struct MarketplaceStackResult: Equatable, Sendable, Decodable {
  public let items: [MarketplaceItem]

  public init(items: [MarketplaceItem]) {
    self.items = items
  }

  private enum CodingKeys: String, CodingKey {
    case curated, store
  }

  public init(from decoder: Decoder) throws {
    let container = try decoder.container(keyedBy: CodingKeys.self)
    let curated = try container.decode([CuratedMarketplaceStackEntry].self, forKey: .curated)
    let store = try container.decode([StoreMarketplaceStackEntry].self, forKey: .store)

    items = curated.compactMap(\.marketplaceItem) + store.compactMap(\.marketplaceItem)
  }
}

private struct CuratedMarketplaceStackEntry: Decodable {
  let marketplaceID: String?
  let marketplaceSlug: String?
  let pluginName: String?
  let manifestName: String?
  let description: String?
  let version: String?
  let enabled: Bool
  let isSystem: Bool
  let isRequired: Bool

  private enum CodingKeys: String, CodingKey {
    case description, version, enabled
    case marketplaceID = "marketplace_id"
    case marketplaceSlug = "marketplace_slug"
    case pluginName = "plugin_name"
    case manifestName = "manifest_name"
    case isSystem = "is_system"
    case isRequired = "is_required"
  }

  init(from decoder: Decoder) throws {
    let container = try decoder.container(keyedBy: CodingKeys.self)
    marketplaceID = container.stringValue(forKey: .marketplaceID)
    marketplaceSlug = container.stringValue(forKey: .marketplaceSlug)
    pluginName = container.stringValue(forKey: .pluginName)
    manifestName = container.stringValue(forKey: .manifestName)
    description = container.stringValue(forKey: .description)
    version = container.stringValue(forKey: .version)
    enabled = container.boolValue(forKey: .enabled) ?? false
    isSystem = container.boolValue(forKey: .isSystem) ?? false
    isRequired = container.boolValue(forKey: .isRequired) ?? false
  }

  var marketplaceItem: MarketplaceItem? {
    guard
      let marketplaceID = marketplaceID?.trimmingCharacters(in: .whitespacesAndNewlines),
      !marketplaceID.isEmpty,
      let pluginName = pluginName?.trimmingCharacters(in: .whitespacesAndNewlines),
      !pluginName.isEmpty
    else {
      return nil
    }

    let installed = enabled || isRequired || isSystem
    guard installed else { return nil }

    return MarketplaceItem(
      id: "curated-\(marketplaceID)/\(pluginName)",
      source: .curated,
      type: .plugin,
      name: pluginName,
      displayName: manifestName,
      description: description,
      version: version,
      installed: true,
      marketplaceSlug: marketplaceSlug ?? marketplaceID,
      marketplaceName: marketplaceID,
      isSystem: isSystem,
      isRequired: isRequired
    )
  }
}

private struct StoreMarketplaceStackEntry: Decodable {
  let entityID: String?
  let type: MarketplaceItemType
  let name: String?
  let description: String?
  let category: String?
  let version: String?
  let ownerUsername: String?
  let invocationName: String?
  let installCount: Int
  let photoURL: String?
  let installedAt: String?
  let visibilityStatus: String?

  private enum CodingKeys: String, CodingKey {
    case type, name, description, category, version
    case entityID = "entity_id"
    case ownerUsername = "owner_username"
    case invocationName = "invocation_name"
    case installCount = "install_count"
    case photoURL = "photo_url"
    case installedAt = "installed_at"
    case visibilityStatus = "visibility_status"
  }

  init(from decoder: Decoder) throws {
    let container = try decoder.container(keyedBy: CodingKeys.self)
    entityID = container.stringValue(forKey: .entityID)
    type = (try? container.decode(MarketplaceItemType.self, forKey: .type)) ?? .unknown("")
    name = container.stringValue(forKey: .name)
    description = container.stringValue(forKey: .description)
    category = container.stringValue(forKey: .category)
    version = container.stringValue(forKey: .version)
    ownerUsername = container.stringValue(forKey: .ownerUsername)
    invocationName = container.stringValue(forKey: .invocationName)
    installCount = container.intValue(forKey: .installCount) ?? 0
    photoURL = container.stringValue(forKey: .photoURL)
    installedAt = container.stringValue(forKey: .installedAt)
    visibilityStatus = container.stringValue(forKey: .visibilityStatus)
  }

  var marketplaceItem: MarketplaceItem? {
    guard
      let entityID = entityID?.trimmingCharacters(in: .whitespacesAndNewlines),
      !entityID.isEmpty,
      let name = name?.trimmingCharacters(in: .whitespacesAndNewlines),
      !name.isEmpty
    else {
      return nil
    }

    return MarketplaceItem(
      id: "flea-\(entityID)",
      source: .flea,
      type: type,
      name: name,
      displayName: name,
      description: description,
      owner: ownerUsername,
      version: version,
      category: category,
      photoURL: photoURL,
      added: installedAt,
      installed: true,
      visibilityStatus: visibilityStatus,
      stackCount: installCount
    )
  }
}

/// Detail response from `agnes marketplace detail --json`.
///
/// The server intentionally uses one schema for curated plugins and Flea
/// entities. Values that only make sense for one source are optional here.
public struct MarketplaceDetail: Equatable, Sendable, Codable {
  public let source: MarketplaceSource?
  public let marketplaceID: String?
  public let marketplaceName: String?
  public let entityID: String?
  public let pluginName: String
  public let manifestName: String
  public let displayName: String?
  public let description: String?
  public let tagline: String?
  public let descriptionLongHTML: String?
  public let version: String?
  public let category: String?
  public let authorName: String?
  public let ownerDisplay: String?
  public let homepage: String?
  public let coverPhotoURL: String?
  public let installed: Bool
  public let installable: Bool
  public let isSystem: Bool
  public let isRequired: Bool
  public let stackCount: Int
  public let skills: [MarketplaceDetailEntry]
  public let agents: [MarketplaceDetailEntry]
  public let commands: [MarketplaceDetailEntry]
  public let hooks: [MarketplaceDetailEntry]
  public let mcps: [MarketplaceDetailEntry]
  public let files: [MarketplaceFile]
  public let docs: [MarketplaceDocument]
  public let useCases: [MarketplaceUseCase]
  public let sampleInteraction: MarketplaceSampleInteraction?

  private enum CodingKeys: String, CodingKey {
    case source, description, tagline, version, category, homepage, installed, installable, skills,
      agents
    case commands, hooks, mcps, files, docs
    case marketplaceID = "marketplace_id"
    case marketplaceName = "marketplace_name"
    case entityID = "entity_id"
    case pluginName = "plugin_name"
    case manifestName = "manifest_name"
    case displayName = "display_name"
    case descriptionLongHTML = "description_long_html"
    case authorName = "author_name"
    case ownerDisplay = "owner_display"
    case coverPhotoURL = "cover_photo_url"
    case isSystem = "is_system"
    case isRequired = "is_required"
    case stackCount = "stack_count"
    case useCases = "use_cases"
    case sampleInteraction = "sample_interaction"
  }

  public init(from decoder: Decoder) throws {
    let container = try decoder.container(keyedBy: CodingKeys.self)
    source = try? container.decodeIfPresent(MarketplaceSource.self, forKey: .source)
    marketplaceID = container.stringValue(forKey: .marketplaceID)
    marketplaceName = container.stringValue(forKey: .marketplaceName)
    entityID = container.stringValue(forKey: .entityID)
    pluginName = container.stringValue(forKey: .pluginName) ?? ""
    manifestName = container.stringValue(forKey: .manifestName) ?? ""
    displayName = container.stringValue(forKey: .displayName)
    description = container.stringValue(forKey: .description)
    tagline = container.stringValue(forKey: .tagline)
    descriptionLongHTML = container.stringValue(forKey: .descriptionLongHTML)
    version = container.stringValue(forKey: .version)
    category = container.stringValue(forKey: .category)
    authorName = container.stringValue(forKey: .authorName)
    ownerDisplay = container.stringValue(forKey: .ownerDisplay)
    homepage = container.stringValue(forKey: .homepage)
    coverPhotoURL = container.stringValue(forKey: .coverPhotoURL)
    installed = container.boolValue(forKey: .installed) ?? false
    installable = container.boolValue(forKey: .installable) ?? true
    isSystem = container.boolValue(forKey: .isSystem) ?? false
    isRequired = container.boolValue(forKey: .isRequired) ?? false
    stackCount = container.intValue(forKey: .stackCount) ?? 0
    skills = (try? container.decode([MarketplaceDetailEntry].self, forKey: .skills)) ?? []
    agents = (try? container.decode([MarketplaceDetailEntry].self, forKey: .agents)) ?? []
    commands = (try? container.decode([MarketplaceDetailEntry].self, forKey: .commands)) ?? []
    hooks = (try? container.decode([MarketplaceDetailEntry].self, forKey: .hooks)) ?? []
    mcps = (try? container.decode([MarketplaceDetailEntry].self, forKey: .mcps)) ?? []
    files = (try? container.decode([MarketplaceFile].self, forKey: .files)) ?? []
    docs = (try? container.decode([MarketplaceDocument].self, forKey: .docs)) ?? []
    useCases = (try? container.decode([MarketplaceUseCase].self, forKey: .useCases)) ?? []
    sampleInteraction = try? container.decodeIfPresent(
      MarketplaceSampleInteraction.self, forKey: .sampleInteraction)
  }
}

/// A named child component in a marketplace bundle. The CLI can return a
/// string or an object for command/MCP entries, so both shapes decode here.
public struct MarketplaceDetailEntry: Equatable, Hashable, Sendable, Codable {
  public let name: String
  public let description: String?
  public let detailURL: String?
  public let type: String?

  private enum CodingKeys: String, CodingKey {
    case name, description, type
    case detailURL = "detail_url"
  }

  public init(from decoder: Decoder) throws {
    if let string = try? decoder.singleValueContainer().decode(String.self) {
      name = string
      description = nil
      detailURL = nil
      type = nil
      return
    }
    let container = try decoder.container(keyedBy: CodingKeys.self)
    name = container.stringValue(forKey: .name) ?? ""
    description = container.stringValue(forKey: .description)
    detailURL = container.stringValue(forKey: .detailURL)
    type = container.stringValue(forKey: .type)
  }
}

public struct MarketplaceFile: Equatable, Hashable, Sendable, Codable {
  public let path: String
  public let size: Int

  private enum CodingKeys: String, CodingKey { case path, size }

  public init(path: String, size: Int) {
    self.path = path
    self.size = size
  }

  public init(from decoder: Decoder) throws {
    let container = try decoder.container(keyedBy: CodingKeys.self)
    path = container.stringValue(forKey: .path) ?? ""
    size = container.intValue(forKey: .size) ?? 0
  }
}

public struct MarketplaceDocument: Equatable, Hashable, Sendable, Codable {
  public let name: String
  public let url: String?

  private enum CodingKeys: String, CodingKey { case name, url }

  public init(from decoder: Decoder) throws {
    let container = try decoder.container(keyedBy: CodingKeys.self)
    name = container.stringValue(forKey: .name) ?? ""
    url = container.stringValue(forKey: .url)
  }
}

public struct MarketplaceUseCase: Equatable, Hashable, Sendable, Codable {
  public let title: String
  public let description: String?
  public let prompt: String?

  private enum CodingKeys: String, CodingKey { case title, description, prompt }

  public init(from decoder: Decoder) throws {
    if let string = try? decoder.singleValueContainer().decode(String.self) {
      title = string
      description = nil
      prompt = nil
      return
    }
    let container = try decoder.container(keyedBy: CodingKeys.self)
    title = container.stringValue(forKey: .title) ?? ""
    description = container.stringValue(forKey: .description)
    prompt = container.stringValue(forKey: .prompt)
  }
}

public struct MarketplaceSampleInteraction: Equatable, Hashable, Sendable, Codable {
  public let user: String?
  public let assistant: String?
  public let assistantHTML: String?

  private enum CodingKeys: String, CodingKey {
    case user, assistant
    case assistantHTML = "assistant_html"
  }

  public init(from decoder: Decoder) throws {
    let container = try decoder.container(keyedBy: CodingKeys.self)
    user = container.stringValue(forKey: .user)
    assistant = container.stringValue(forKey: .assistant)
    assistantHTML = container.stringValue(forKey: .assistantHTML)
  }
}

extension KeyedDecodingContainer {
  fileprivate func stringValue(forKey key: Key) -> String? {
    guard contains(key), (try? decodeNil(forKey: key)) != true else { return nil }
    if let value = try? decode(String.self, forKey: key) { return value }
    if let value = try? decode(Int.self, forKey: key) { return String(value) }
    if let value = try? decode(Double.self, forKey: key) { return String(value) }
    if let value = try? decode(Bool.self, forKey: key) { return String(value) }
    return nil
  }

  fileprivate func intValue(forKey key: Key) -> Int? {
    guard contains(key), (try? decodeNil(forKey: key)) != true else { return nil }
    if let value = try? decode(Int.self, forKey: key) { return value }
    if let value = try? decode(Double.self, forKey: key) { return Int(value) }
    if let value = try? decode(String.self, forKey: key) {
      return Int(value) ?? Double(value).map(Int.init)
    }
    return nil
  }

  fileprivate func doubleValue(forKey key: Key) -> Double? {
    guard contains(key), (try? decodeNil(forKey: key)) != true else { return nil }
    if let value = try? decode(Double.self, forKey: key) { return value }
    if let value = try? decode(Int.self, forKey: key) { return Double(value) }
    if let value = try? decode(String.self, forKey: key) { return Double(value) }
    return nil
  }

  fileprivate func boolValue(forKey key: Key) -> Bool? {
    guard contains(key), (try? decodeNil(forKey: key)) != true else { return nil }
    if let value = try? decode(Bool.self, forKey: key) { return value }
    if let value = try? decode(Int.self, forKey: key) { return value != 0 }
    if let value = try? decode(String.self, forKey: key) {
      switch value.lowercased() {
      case "true", "1", "yes": return true
      case "false", "0", "no": return false
      default: return nil
      }
    }
    return nil
  }
}
