namespace :constraints do
  desc 'Extracts database constraints for an application'

  task extract: :environment do
    string_like_types = %i[string text]
    has_macros = %i[has_one has_many]

    conn = ActiveRecord::Base.connection

    Rails.application.eager_load!
    models = ActiveRecord::Base.descendants.select { |m| m.table_name.present? }
    table_names = models.map(&:table_name).uniq

    puts '// Primary keys.'
    table_names.each do |table_name|
      primary_key = conn.primary_key(table_name)
      puts "{ type = \"unique\", tbl = \"#{table_name}\", cols = [\"#{primary_key}\"] },"
    end
    puts

    puts '// Uniqueness.'
    table_names.each do |table_name|
      indexes = conn.indexes(table_name)
      indexes.select(&:unique).each do |index|
        columns = index.columns
        puts "{ type = \"unique\", tbl = \"#{table_name}\", cols = [#{columns.map { |c| "\"#{c}\"" }.join(', ')}] },"
      end
    end
    puts

    puts '// Foreign keys.'
    models.each do |model|
      from_tbl = model.table_name
      model.reflect_on_all_associations(:belongs_to).each do |association|
        from_col = association.foreign_key
        if association.polymorphic?
          next if association.options[:optional]

          from_type_col = association.foreign_type
          print_non_null(from_tbl, from_type_col)

          inverses = models.flat_map(&:reflect_on_all_associations).select do |a|
            has_macros.include?(a.macro) && a.options[:as] == association.name && a.klass == model
          end
          next if inverses.empty? # TODO(zhangwen): what's the deal with ActiveAdmin Comment?

          all_type_names = inverses.map { |a| a.active_record.name }
          puts "{ type = \"one-of-string\", tbl = \"#{from_tbl}\", col = \"#{from_type_col}\", allowed-values = [#{all_type_names.map { |n| "\"#{n}\"" }.join(', ')}] },"

          if inverses.length == 1
            a = inverses.first
            to_tbl = a.active_record.table_name
            to_col = a.join_foreign_key
            puts "{ type = \"foreign-key-non-null\", from-tbl = \"#{from_tbl}\", from-col = \"#{from_col}\", to-tbl = \"#{to_tbl}\", to-col = \"#{to_col}\" },"
          end
        else
          to_tbl = association.klass.table_name
          to_col = association.association_primary_key

          type = association.options[:optional] ? 'foreign-key' : 'foreign-key-non-null'
          puts "{ type = \"#{type}\", from-tbl = \"#{from_tbl}\", from-col = \"#{from_col}\", to-tbl = \"#{to_tbl}\", to-col = \"#{to_col}\" },"
        end
      end
    end
    puts

    puts '// Constrain `created_at` and `updated_at` to be NOT NULL.'
    table_names.each do |table_name|
      columns = conn.columns(table_name)
      print_non_null(table_name, 'created_at') if columns.any? { |c| c.name == 'created_at' }
      print_non_null(table_name, 'updated_at') if columns.any? { |c| c.name == 'updated_at' }
    end
    puts

    puts '// Enums.'
    models.each do |model|
      table_name = model.table_name
      model.defined_enums.each do |attr, defs|
        next unless defs.is_a?(Hash)

        column = model.columns_hash[attr.to_s]
        next unless column

        values = defs.values
        next unless values.all?(Integer)

        puts "{ type = \"one-of-int\", tbl = \"#{table_name}\", col = \"#{column.name}\", values = [#{values.join(', ')}] },"
      end
    end
    puts

    puts '// Presence.'
    models.each do |model|
      model.validators.each do |validator|
        next unless validator.is_a?(ActiveModel::Validations::PresenceValidator) ||
                    (validator.is_a?(ActiveModel::Validations::LengthValidator) && validator.options[:minimum]&.positive?)

        attributes = validator.attributes
        next unless attributes.size == 1 # There should be one and only one attribute. TODO(zhangwen): Validate this.

        attribute = attributes.first
        column = model.columns_hash[attribute.to_s]
        next unless column

        # TODO(zhangwen): Not needed if the column is already constrained to be NOT NULL.
        print_non_null(model.table_name, column.name)

        # If the attribute is string or text, also constrain it to be not empty.
        if string_like_types.include?(column.type)
          puts "{ type = \"string-is-not-empty\", tbl = \"#{model.table_name}\", col = \"#{column.name}\" },"
        end
      end
    end
  end

  def print_non_null(tbl, col)
    puts "{ type = \"non-null\", tbl = \"#{tbl}\", col = \"#{col}\" },"
  end
end
